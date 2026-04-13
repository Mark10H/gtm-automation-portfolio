"""
checkpoint_manager.py
---------------------
Session state manager for the outbound pipeline.
Enables resumable batches — if a run is interrupted mid-batch, the pipeline
can pick up from the last completed step without re-processing accounts.

Commands:
  write    <checkpoint.json> <payload.json>   Write/update checkpoint state
  read     <checkpoint.json>                  Print checkpoint summary to stderr
  validate <checkpoint.json>                  Validate checkpoint and print exit status
  complete <checkpoint.json>                  Mark session as complete

Exit codes (validate):
  0  — valid, resumable checkpoint found
  1  — not_found or already_complete
  2  — stale checkpoint (>24 hours old)

Checkpoint schema:
{
  "session_id": "...",
  "rep_name": "...",
  "rep_owner_id": "...",
  "headcount_range": [50, 500],
  "status": "in_progress|complete",
  "created_at": "ISO datetime",
  "updated_at": "ISO datetime",
  "approved_accounts": [...],         # accounts approved for research
  "in_progress": "company_id|null",   # account currently being processed
  "already_researched_this_session": [...],
  "contacts_pulled": {...},           # keyed by company_id
  "emails_generated": {...},          # keyed by company_id
  "hubspot_updated": [...]            # company IDs written back to HubSpot
}

Usage:
  python checkpoint_manager.py write checkpoint.json payload.json
  python checkpoint_manager.py read checkpoint.json
  python checkpoint_manager.py validate checkpoint.json
  python checkpoint_manager.py complete checkpoint.json
"""

import json
import sys
import os
from datetime import datetime, timezone, timedelta

STALE_THRESHOLD_HOURS = 24


def load_checkpoint(path: str) -> dict:
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def save_checkpoint(path: str, data: dict):
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def cmd_write(checkpoint_path: str, payload_path: str):
    """Merge payload into existing checkpoint (or create new one)."""
    with open(payload_path, "r") as f:
        payload = json.load(f)

    existing = load_checkpoint(checkpoint_path) or {
        "session_id": payload.get("session_id", datetime.now().strftime("%Y%m%d_%H%M%S")),
        "status": "in_progress",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "already_researched_this_session": [],
        "contacts_pulled": {},
        "emails_generated": {},
        "hubspot_updated": [],
        "approved_accounts": [],
        "in_progress": None,
    }

    # Merge payload into checkpoint (payload keys win)
    existing.update(payload)
    save_checkpoint(checkpoint_path, existing)
    print(f"Checkpoint updated: {checkpoint_path}", file=sys.stderr)


def cmd_read(checkpoint_path: str):
    """Print a human-readable checkpoint summary."""
    cp = load_checkpoint(checkpoint_path)
    if not cp:
        print("No checkpoint found.", file=sys.stderr)
        sys.exit(1)

    approved = cp.get("approved_accounts", [])
    researched = cp.get("already_researched_this_session", [])
    remaining = [a for a in approved if a.get("company_id") not in researched]
    in_progress = cp.get("in_progress")

    print(f"Session: {cp.get('session_id')}", file=sys.stderr)
    print(f"Rep: {cp.get('rep_name')} | Status: {cp.get('status')}", file=sys.stderr)
    print(f"Accounts done: {len(researched)} | Remaining: {len(remaining)}", file=sys.stderr)
    if in_progress:
        print(f"In progress: {in_progress}", file=sys.stderr)
    print(f"Last updated: {cp.get('updated_at')}", file=sys.stderr)


def cmd_validate(checkpoint_path: str):
    """
    Validate checkpoint and exit with appropriate code.
    Exit 0 = resumable, Exit 1 = not found/complete, Exit 2 = stale
    """
    cp = load_checkpoint(checkpoint_path)

    if not cp:
        print("not_found", file=sys.stderr)
        sys.exit(1)

    if cp.get("status") == "complete":
        print("already_complete", file=sys.stderr)
        sys.exit(1)

    # Check staleness
    updated_at = cp.get("updated_at") or cp.get("created_at")
    if updated_at:
        try:
            updated = datetime.fromisoformat(updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
            if age_hours > STALE_THRESHOLD_HOURS:
                print(f"stale ({age_hours:.1f} hours old)", file=sys.stderr)
                sys.exit(2)
        except ValueError:
            pass  # Unparseable date — treat as valid

    print("valid", file=sys.stderr)
    sys.exit(0)


def cmd_complete(checkpoint_path: str):
    """Mark the session as complete."""
    cp = load_checkpoint(checkpoint_path)
    if not cp:
        print("No checkpoint found to complete.", file=sys.stderr)
        sys.exit(1)

    cp["status"] = "complete"
    cp["in_progress"] = None
    save_checkpoint(checkpoint_path, cp)
    print(f"Session marked complete: {cp.get('session_id')}", file=sys.stderr)


# ── CLI Entry Point ────────────────────────────────────────────────────────────

COMMANDS = {
    "write": lambda args: cmd_write(args[0], args[1]),
    "read": lambda args: cmd_read(args[0]),
    "validate": lambda args: cmd_validate(args[0]),
    "complete": lambda args: cmd_complete(args[0]),
}

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python checkpoint_manager.py <command> <checkpoint.json> [payload.json]")
        print("Commands: write, read, validate, complete")
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    if command not in COMMANDS:
        print(f"Unknown command: {command}. Use: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    COMMANDS[command](args)
