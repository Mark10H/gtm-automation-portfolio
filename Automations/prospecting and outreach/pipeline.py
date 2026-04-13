"""
pipeline.py
-----------
Main orchestrator for the GTM outbound automation pipeline.
Coordinates all steps from HubSpot account pull through HubSpot task creation.

Pipeline steps:
  1. Resolve rep identity + pull accounts from HubSpot
  2. Filter accounts against ICP SOP rules
  1.5. Score filtered accounts (ZoomInfo intent + web research)
  3. Live web research per top account
  4. Build account intelligence (6 fields)
  4.5. ZoomInfo contact enrichment
  5. Write personalized 4-step email sequences
  6. Rep approval gate
  7. Write HubSpot notes
  8. Create HubSpot tasks (full/email/phone tracks)

Special commands: resume, territory, audit

Usage:
  python pipeline.py --rep "First Last" --headcount "100-500"
  python pipeline.py --rep "First Last" --headcount "no preference"
  python pipeline.py --resume
"""

import argparse
import json
import os
import sys
from datetime import datetime

from filter_accounts import filter_accounts
from score_accounts import score_accounts, score_accounts_web_only
from hubspot_client import HubSpotClient
from checkpoint_manager import load_checkpoint, save_checkpoint, cmd_validate


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config(path="config.json") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"config.json not found. Copy config.example.json to config.json and fill in your credentials."
        )
    with open(path) as f:
        return json.load(f)


# ── Session Directory ──────────────────────────────────────────────────────────

def get_session_dir(session_id: str) -> str:
    path = f"sessions/{session_id}"
    os.makedirs(path, exist_ok=True)
    return path


# ── Step 1: Pull Accounts ──────────────────────────────────────────────────────

def step1_pull_accounts(hubspot: HubSpotClient, rep_name: str, session_dir: str) -> tuple:
    """
    Resolves the rep's HubSpot owner ID and pulls their assigned accounts.
    Returns (rep_owner_id, accounts_list).
    """
    print(f"\n[Step 1] Resolving rep: {rep_name}")

    owner_id = hubspot.resolve_rep_owner_id(rep_name)
    if not owner_id:
        print(f"ERROR: Could not find HubSpot owner for '{rep_name}'. "
              f"Verify name matches HubSpot exactly.")
        sys.exit(1)

    print(f"[Step 1] Owner ID resolved: {owner_id}")

    # Load dual-owner config
    dual_owner_reps = []
    rep_config_path = "references/rep-config.json"
    if os.path.exists(rep_config_path):
        with open(rep_config_path) as f:
            rep_config = json.load(f)
            dual_owner_reps = rep_config.get("dual_owner_reps", [])

    also_owner_b = rep_name in dual_owner_reps
    accounts = hubspot.pull_accounts_by_owner(owner_id, also_owner_b=also_owner_b)

    print(f"[Step 1] Pulled {len(accounts)} accounts for {rep_name}")

    # Save raw accounts
    with open(f"{session_dir}/accounts_raw.json", "w") as f:
        json.dump(accounts, f, indent=2)

    return owner_id, accounts


# ── Step 2: Filter Accounts ────────────────────────────────────────────────────

def step2_filter_accounts(accounts: list, headcount_min, headcount_max, session_dir: str) -> list:
    """
    Applies ICP SOP filters. Returns qualified accounts list.
    """
    print(f"\n[Step 2] Filtering {len(accounts)} accounts...")

    filter_input = {
        "accounts": accounts,
        "headcount_min": headcount_min,
        "headcount_max": headcount_max,
        "batch_limit": 100,
    }

    with open(f"{session_dir}/filter_input.json", "w") as f:
        json.dump(filter_input, f, indent=2)

    result = filter_accounts(filter_input)

    with open(f"{session_dir}/filter_output.json", "w") as f:
        json.dump(result, f, indent=2)

    qualified = result["qualified"]
    excluded = result["excluded"]

    print(f"[Step 2] {len(qualified)} qualified, {len(excluded)} excluded")

    if not qualified:
        print("\nNo qualified accounts after SOP filtering.")
        print("Exclusion summary:")
        reasons = {}
        for a in excluded:
            r = a.get("exclusion_reason", "Unknown")
            reasons[r] = reasons.get(r, 0) + 1
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {count}x {reason}")
        sys.exit(0)

    return qualified


# ── Step 1.5: Enrichment Scoring ──────────────────────────────────────────────

def step1_5_score_accounts(qualified: list, session_dir: str, zi_available: bool = True) -> list:
    """
    Scores qualified accounts using ZoomInfo signals + live web research.
    Returns top 10 accounts with enrichment score > 60.

    Note: In the full production system, ZoomInfo MCP calls and web research
    are run here via Claude tool use before this function is called.
    This function handles scoring given pre-assembled intent_data.json.
    """
    print(f"\n[Step 1.5] Scoring {len(qualified)} accounts...")

    intent_data_path = f"{session_dir}/intent_data.json"

    if not os.path.exists(intent_data_path):
        print(f"WARNING: intent_data.json not found at {intent_data_path}. "
              f"Run ZoomInfo enrichment and web research first.")
        return []

    with open(intent_data_path) as f:
        intent_data = json.load(f)

    if zi_available:
        result = score_accounts(intent_data)
    else:
        print("[Step 1.5] ZoomInfo not connected — scoring on web research only")
        result = score_accounts_web_only(intent_data)

    with open(f"{session_dir}/scored_accounts.json", "w") as f:
        json.dump(result, f, indent=2)

    top_10 = result["ranked"]
    print(f"[Step 1.5] Top {len(top_10)} accounts selected (score > 60)")

    for i, a in enumerate(top_10, 1):
        print(f"  {i}. {a['company_name']} — score: {a['enrichment_score']}")

    return top_10


# ── Resume Flow ────────────────────────────────────────────────────────────────

def resume_session(session_dir: str, hubspot: HubSpotClient):
    """
    Loads the last checkpoint and resumes the pipeline from where it left off.
    Skips already-completed steps (contacts pulled, emails generated, HubSpot written).
    """
    checkpoint_path = f"{session_dir}/checkpoint.json"
    cp = load_checkpoint(checkpoint_path)

    if not cp:
        print("No saved session found. Run with --rep to start a new batch.")
        sys.exit(0)

    if cp.get("status") == "complete":
        print(f"Session {cp['session_id']} is complete. Start a new batch with --rep.")
        sys.exit(0)

    print(f"\nResuming session: {cp['session_id']}")
    print(f"Rep: {cp['rep_name']}")

    researched = cp.get("already_researched_this_session", [])
    approved = cp.get("approved_accounts", [])
    remaining = [a for a in approved if a.get("company_id") not in researched]
    in_progress = cp.get("in_progress")

    print(f"Accounts completed: {len(researched)} | Remaining: {len(remaining)}")
    if in_progress:
        print(f"Resuming in-progress account: {in_progress}")

    return cp


# ── Main Entry Point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GTM Outbound Automation Pipeline")
    parser.add_argument("--rep", type=str, help="Rep full name (e.g. 'Jane Smith')")
    parser.add_argument("--headcount", type=str, default="no preference",
                        help="Headcount range (e.g. '100-500' or 'no preference')")
    parser.add_argument("--resume", action="store_true", help="Resume last checkpoint")
    parser.add_argument("--session", type=str, help="Specific session ID to resume")
    args = parser.parse_args()

    config = load_config()
    hubspot = HubSpotClient(config)

    # ── Parse headcount range ──────────────────────────────────────────────────
    headcount_min, headcount_max = None, None
    if args.headcount.lower() != "no preference":
        try:
            parts = args.headcount.replace(" ", "").split("-")
            headcount_min = int(parts[0])
            headcount_max = int(parts[1])
        except (ValueError, IndexError):
            print(f"Invalid headcount format: '{args.headcount}'. Use '100-500' or 'no preference'.")
            sys.exit(1)

    # ── Session setup ──────────────────────────────────────────────────────────
    session_id = args.session or datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = get_session_dir(session_id)

    if args.resume:
        resume_session(session_dir, hubspot)
        return

    if not args.rep:
        parser.print_help()
        sys.exit(1)

    # ── Full pipeline run ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"GTM Outbound Pipeline — Session {session_id}")
    print(f"Rep: {args.rep} | Headcount: {args.headcount}")
    print(f"{'='*60}")

    # Step 1: Pull accounts
    owner_id, accounts = step1_pull_accounts(hubspot, args.rep, session_dir)

    # Write initial checkpoint
    save_checkpoint(f"{session_dir}/checkpoint.json", {
        "session_id": session_id,
        "rep_name": args.rep,
        "rep_owner_id": owner_id,
        "headcount_range": [headcount_min, headcount_max],
    })

    # Step 2: Filter
    qualified = step2_filter_accounts(accounts, headcount_min, headcount_max, session_dir)

    # Steps 1.5 onward require ZoomInfo + web research data to be assembled first.
    # In production, Claude orchestrates these via tool use (ZoomInfo MCP + web_search).
    # The scoring, research, intelligence, sequencing, and HubSpot write steps
    # are then executed by Claude using this pipeline's scripts and the skill's prompts.
    print(f"\n[Next] Run ZoomInfo enrichment and web research on {len(qualified)} qualified accounts.")
    print(f"[Next] Assemble {session_dir}/intent_data.json, then run score_accounts.py")
    print(f"[Session dir] {session_dir}/")


if __name__ == "__main__":
    main()
