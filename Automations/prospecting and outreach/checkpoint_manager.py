"""
checkpoint_manager.py — Moxo BDR Session Checkpoint Manager
============================================================
Writes, reads, validates, and displays session checkpoints so a BDR
can resume a research session exactly where it left off — even across
Claude sessions, restarts, or context resets.

USAGE:
    # Bootstrap or update a checkpoint from a patch payload JSON
    python scripts/checkpoint_manager.py write <checkpoint_path> <payload_json_path>

    # Read and display a checkpoint summary to stderr, full JSON to stdout
    python scripts/checkpoint_manager.py read <checkpoint_path>

    # Validate checkpoint freshness
    #   exit 0 = fresh + active   → safe to resume
    #   exit 2 = stale (> 24h)    → ask rep whether to resume or restart
    #   exit 1 = not found / done → start fresh
    python scripts/checkpoint_manager.py validate <checkpoint_path> [--stale-hours=N]

    # Mark a checkpoint as complete (end of session)
    python scripts/checkpoint_manager.py complete <checkpoint_path>

─────────────────────────────────────────────────────────────
CHECKPOINT FILE: checkpoint.json  (session working directory)
─────────────────────────────────────────────────────────────

PAYLOAD SCHEMA (passed to `write`):
{
  // All fields are optional — only include what changed
  "rep_name":                "Spencer Johnson",
  "headcount_min":           50,
  "headcount_max":           500,
  "session_dir":             "/sessions/abc123",
  "filter_summary":          { ...summary block from filter_accounts.py... },
  "qualified_pool":          [ ...company objects... ],
  "excluded_accounts":       [ ...excluded objects... ],
  "intent_scored":           true,
  "ranked_accounts":         [ ...scored account objects... ],
  "approved_accounts":       [ "Acme Financial", "Summit Insurance" ],
  "in_progress":             "Acme Financial",           // null to clear
  "researched_this_session": [ "Acme Financial" ],       // appended, not replaced
  "contacts_pulled":         { "Acme Financial": [...] },
  "emails_generated":        { "Acme Financial": [...] },
  "hubspot_updated":         [ "Acme Financial" ],
  "audit_log":               [ { "company": "...", "action": "...", "ts": "..." } ]
}

OUTPUT JSON SCHEMA (from `read` and `validate`):
{
  "schema_version":          "1.0",
  "session_id":              "20260408-143022",
  "created_at":              "2026-04-08T14:30:22Z",
  "updated_at":              "2026-04-08T15:12:47Z",
  "status":                  "active",           // "active" | "complete"
  "rep_name":                "Spencer Johnson",
  "headcount_min":           50,
  "headcount_max":           500,
  "session_dir":             "/sessions/abc123",
  "filter_summary":          { ... },
  "qualified_pool":          [ ... ],
  "excluded_accounts":       [ ... ],
  "intent_scored":           true,
  "ranked_accounts":         [ ... ],
  "approved_accounts":       [ "Acme Financial", "Summit Insurance" ],
  "researched_this_session": [ "Acme Financial" ],
  "in_progress":             null,
  "contacts_pulled":         { "Acme Financial": [...] },
  "emails_generated":        { "Acme Financial": [...] },
  "hubspot_updated":         [ "Acme Financial" ],
  "audit_log":               [ ... ]
}
"""

import sys
import json
import os
from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION      = "1.0"
DEFAULT_STALE_HOURS = 24

# List fields that accumulate (append + deduplicate strings, or extend objects)
EXTEND_STRING_LISTS = {
    "researched_this_session",
    "hubspot_updated",
    "approved_accounts",
}

# List fields that are replaced wholesale (latest value wins)
REPLACE_LISTS = {
    "qualified_pool",
    "excluded_accounts",
    "ranked_accounts",
    "audit_log",
}

# Dict fields that are key-merged (new keys added, existing keys overwritten)
MERGE_DICTS = {
    "contacts_pulled",
    "emails_generated",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_checkpoint(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_checkpoint(path: str, data: dict) -> None:
    data["updated_at"]     = now_iso()
    data["schema_version"] = SCHEMA_VERSION
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def checkpoint_age_hours(cp: dict) -> float | None:
    ts_str = cp.get("updated_at") or cp.get("created_at")
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except ValueError:
        return None


def is_stale(cp: dict, stale_hours: float = DEFAULT_STALE_HOURS) -> bool:
    age = checkpoint_age_hours(cp)
    return age is None or age > stale_hours


# ---------------------------------------------------------------------------
# Schema builder
# ---------------------------------------------------------------------------

def new_checkpoint(rep_name: str,
                   headcount_min=None,
                   headcount_max=None,
                   session_dir: str = "") -> dict:
    return {
        "schema_version":          SCHEMA_VERSION,
        "session_id":              datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        "created_at":              now_iso(),
        "updated_at":              now_iso(),
        "status":                  "active",
        "rep_name":                rep_name,
        "headcount_min":           headcount_min,
        "headcount_max":           headcount_max,
        "session_dir":             session_dir,
        # Step 2 — filter
        "filter_summary":          None,
        "qualified_pool":          [],
        "excluded_accounts":       [],
        # Step 1.5 — intent scoring
        "intent_scored":           False,
        "ranked_accounts":         [],
        # Rep-approved batch for this run
        "approved_accounts":       [],
        # Per-account progress
        "researched_this_session": [],
        "in_progress":             None,
        "contacts_pulled":         {},
        "emails_generated":        {},
        "hubspot_updated":         [],
        # Audit trail
        "audit_log":               [],
    }


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

def merge_patch(checkpoint: dict, patch: dict) -> dict:
    """
    Apply patch to checkpoint with field-type-aware merge rules:
      - EXTEND_STRING_LISTS : append new strings, deduplicate
      - REPLACE_LISTS       : replace wholesale
      - MERGE_DICTS         : update keys
      - Everything else     : overwrite scalar
    """
    for key, value in patch.items():
        if key in EXTEND_STRING_LISTS and isinstance(value, list):
            existing = checkpoint.get(key, [])
            seen     = set(existing)
            for item in value:
                if isinstance(item, str) and item not in seen:
                    existing.append(item)
                    seen.add(item)
            checkpoint[key] = existing

        elif key in REPLACE_LISTS and isinstance(value, list):
            checkpoint[key] = value

        elif key in MERGE_DICTS and isinstance(value, dict):
            existing = checkpoint.get(key, {})
            existing.update(value)
            checkpoint[key] = existing

        else:
            checkpoint[key] = value

    return checkpoint


# ---------------------------------------------------------------------------
# Dashboard formatter
# ---------------------------------------------------------------------------

def format_resume_summary(cp: dict) -> str:
    W   = 56
    sep = "═" * W
    div = "─" * W

    rep        = cp.get("rep_name", "Unknown Rep")
    session_id = cp.get("session_id", "n/a")
    status     = cp.get("status", "active").upper()

    researched = cp.get("researched_this_session", [])
    approved   = cp.get("approved_accounts", [])
    in_prog    = cp.get("in_progress")
    emails     = cp.get("emails_generated", {})
    hubspot    = cp.get("hubspot_updated", [])
    scored     = cp.get("intent_scored", False)
    hmin       = cp.get("headcount_min")
    hmax       = cp.get("headcount_max")

    # Remaining = approved but not yet researched and not in-progress
    remaining = [
        a for a in approved
        if a not in researched and a != in_prog
    ]

    # Age warning
    age_h = checkpoint_age_hours(cp)
    age_str = ""
    if age_h is not None and age_h >= 0.5:
        if age_h < 1:
            age_str = f"  ⚠  Last saved {int(age_h * 60)}m ago"
        elif age_h < 24:
            age_str = f"  ⚠  Last saved {age_h:.1f}h ago"
        else:
            age_str = f"  ⚠  Last saved {age_h:.1f}h ago — checkpoint may be stale"

    lines = [
        "",
        sep,
        f"  Session Resume — {rep}",
        f"  Session ID : {session_id}",
        f"  Status     : {status}",
    ]
    if age_str:
        lines.append(age_str)
    lines += [
        sep,
        f"  {'Accounts approved this session':<32} {len(approved):>4}",
        f"  {'Accounts fully researched':<32} {len(researched):>4}",
        f"  {'Email sequences generated':<32} {len(emails):>4}",
        f"  {'Written back to HubSpot':<32} {len(hubspot):>4}",
        f"  {'Intent scoring complete':<32} {'Yes' if scored else 'No':>4}",
    ]

    if hmin or hmax:
        lo = str(hmin) if hmin is not None else "any"
        hi = str(hmax) if hmax is not None else "any"
        lines.append(f"  {'Headcount filter':<32} {lo+'–'+hi:>4}")

    if in_prog:
        lines += [
            div,
            f"  ⟳  Interrupted mid-account: {in_prog}",
            f"     Resume research on this account first.",
        ]

    if remaining:
        lines += [div, f"  Remaining in approved batch ({len(remaining)}):"]
        for co in remaining:
            check = "✓" if co in researched else "○"
            lines.append(f"    {check} {co}")
    elif not in_prog and status == "ACTIVE":
        lines += [div, "  ✓  All approved accounts researched."]
        lines.append("     Type run batch to queue the next batch.")

    filter_summary = cp.get("filter_summary")
    if filter_summary:
        pool_size = filter_summary.get("total_qualified", len(cp.get("qualified_pool", [])))
        lines += [div, f"  Qualified pool (pre-scoring): {pool_size} accounts"]

    lines += ["", sep, ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def cmd_write(checkpoint_path: str, payload_path: str) -> None:
    if not os.path.exists(payload_path):
        print(f"ERROR: Payload file not found: {payload_path}", file=sys.stderr)
        sys.exit(1)

    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    cp = load_checkpoint(checkpoint_path)

    if cp is None:
        # Bootstrap new checkpoint from payload
        cp = new_checkpoint(
            rep_name      = payload.get("rep_name", "Unknown Rep"),
            headcount_min = payload.get("headcount_min"),
            headcount_max = payload.get("headcount_max"),
            session_dir   = payload.get("session_dir", ""),
        )

    cp = merge_patch(cp, payload)
    save_checkpoint(checkpoint_path, cp)

    summary = format_resume_summary(cp)
    print(summary, file=sys.stderr)
    print(f"Checkpoint saved → {checkpoint_path}", file=sys.stderr)
    print(json.dumps({"status": "ok", "checkpoint_path": checkpoint_path}, indent=2))


def cmd_read(checkpoint_path: str) -> None:
    cp = load_checkpoint(checkpoint_path)
    if cp is None:
        print(f"ERROR: No checkpoint found at {checkpoint_path}", file=sys.stderr)
        sys.exit(1)

    print(format_resume_summary(cp), file=sys.stderr)
    print(json.dumps(cp, indent=2, ensure_ascii=False))


def cmd_validate(checkpoint_path: str, stale_hours: float = DEFAULT_STALE_HOURS) -> None:
    """
    Exit codes:
      0 → valid + active + fresh   (safe to resume)
      1 → not found or complete    (start fresh)
      2 → stale (> stale_hours)   (ask rep)
    """
    cp = load_checkpoint(checkpoint_path)

    if cp is None:
        print(json.dumps({"valid": False, "reason": "not_found"}))
        sys.exit(1)

    if cp.get("status") == "complete":
        print(json.dumps({
            "valid":   False,
            "reason":  "already_complete",
            "summary": {
                "rep_name":               cp.get("rep_name"),
                "session_id":             cp.get("session_id"),
                "researched_this_session":cp.get("researched_this_session", []),
            },
        }))
        sys.exit(1)

    age_h = checkpoint_age_hours(cp)
    if is_stale(cp, stale_hours):
        print(json.dumps({
            "valid":      False,
            "reason":     "stale",
            "age_hours":  round(age_h, 1) if age_h is not None else None,
            "updated_at": cp.get("updated_at"),
            "checkpoint": cp,
        }))
        sys.exit(2)

    print(json.dumps({
        "valid":      True,
        "age_hours":  round(age_h, 1) if age_h is not None else 0,
        "checkpoint": cp,
    }))
    sys.exit(0)


def cmd_complete(checkpoint_path: str) -> None:
    cp = load_checkpoint(checkpoint_path)
    if cp is None:
        print(f"ERROR: No checkpoint found at {checkpoint_path}", file=sys.stderr)
        sys.exit(1)

    cp["status"]     = "complete"
    cp["in_progress"] = None
    save_checkpoint(checkpoint_path, cp)

    researched = cp.get("researched_this_session", [])
    hubspot    = cp.get("hubspot_updated", [])
    print(
        f"Session complete — {len(researched)} accounts researched, "
        f"{len(hubspot)} written to HubSpot.",
        file=sys.stderr,
    )
    print(json.dumps({"status": "complete", "checkpoint_path": checkpoint_path}))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]

    if command == "write":
        if len(sys.argv) < 4:
            print("Usage: checkpoint_manager.py write <checkpoint_path> <payload_json>",
                  file=sys.stderr)
            sys.exit(1)
        cmd_write(sys.argv[2], sys.argv[3])

    elif command == "read":
        if len(sys.argv) < 3:
            print("Usage: checkpoint_manager.py read <checkpoint_path>", file=sys.stderr)
            sys.exit(1)
        cmd_read(sys.argv[2])

    elif command == "validate":
        if len(sys.argv) < 3:
            print("Usage: checkpoint_manager.py validate <checkpoint_path> [--stale-hours=N]",
                  file=sys.stderr)
            sys.exit(1)
        stale_hours = DEFAULT_STALE_HOURS
        for arg in sys.argv[3:]:
            if arg.startswith("--stale-hours="):
                try:
                    stale_hours = float(arg.split("=", 1)[1])
                except ValueError:
                    pass
        cmd_validate(sys.argv[2], stale_hours)

    elif command == "complete":
        if len(sys.argv) < 3:
            print("Usage: checkpoint_manager.py complete <checkpoint_path>", file=sys.stderr)
            sys.exit(1)
        cmd_complete(sys.argv[2])

    else:
        print(f"ERROR: Unknown command '{command}'", file=sys.stderr)
        print("Commands: write | read | validate | complete", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
