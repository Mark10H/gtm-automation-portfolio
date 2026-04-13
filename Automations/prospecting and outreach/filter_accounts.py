"""
filter_accounts.py
------------------
Filters HubSpot company records against ICP (Ideal Customer Profile) criteria.
Takes a JSON input file of raw account records and outputs qualified vs excluded accounts.

Input schema:
{
  "accounts": [...],        # list of HubSpot company records
  "headcount_min": 50,      # rep-specified headcount floor (null = no filter)
  "headcount_max": 500,     # rep-specified headcount ceiling (null = no filter)
  "batch_limit": 100        # max accounts to pass through for scoring
}

Output schema:
{
  "qualified": [...],       # accounts that passed all filters
  "excluded": [...]         # accounts with exclusion reasons logged
}

Usage:
    python filter_accounts.py input.json output.json
"""

import json
import sys

# ── ICP Configuration ──────────────────────────────────────────────────────────

TARGET_INDUSTRIES = {
    "real estate",
    "financial services",
    "banking",
    "computer software",
    "it and services",
    "insurance",
    "manufacturing",
    "law practice",
    "legal services",
    "business services",
    "investment management",
    "investment banking",
    "commercial real estate",
}

NEGATIVE_KEYWORDS = [
    "university", "school", "college", "non-profit", "nonprofit",
    "government", "defense", "weapons", ".edu", "school district",
]

GEOGRAPHY_ALLOWLIST = {"united states", "us", "usa", "canada", "ca"}


# ── Filter Functions ───────────────────────────────────────────────────────────

def check_headcount(account, headcount_min, headcount_max):
    """Returns (passed: bool, reason: str)"""
    if headcount_min is None and headcount_max is None:
        return True, None

    employees = account.get("number_of_employees")
    if employees is None:
        # Blank headcount: pass through (field-blank handling per SOP)
        return True, None

    try:
        employees = int(employees)
    except (ValueError, TypeError):
        return True, None

    if headcount_min and employees < headcount_min:
        return False, f"Excluded — Outside Headcount Range ({employees} employees, rep range {headcount_min}–{headcount_max})"
    if headcount_max and employees > headcount_max:
        return False, f"Excluded — Outside Headcount Range ({employees} employees, rep range {headcount_min}–{headcount_max})"

    return True, None


def check_geography(account):
    """Returns (passed: bool, reason: str)"""
    country = (account.get("country") or "").strip().lower()
    if country == "":
        # Blank country: pass through with a note (reviewed at research stage)
        return True, None
    if country not in GEOGRAPHY_ALLOWLIST:
        return False, "Excluded — Outside Geography"
    return True, None


def check_active_customer(account):
    """Returns (passed: bool, reason: str)"""
    status = (account.get("is_active_customer") or "").strip().lower()
    if status == "yes":
        return False, "Excluded — Active Customer"
    return True, None


def check_already_enriched(account):
    """Returns (passed: bool, reason: str)"""
    enriched = (account.get("ai_claude_enriched") or "").strip().lower()
    if enriched == "yes":
        return False, "Excluded — Already Enriched"
    return True, None


def check_open_deals(account):
    """Returns (passed: bool, reason: str)"""
    open_deals = account.get("associated_open_deals", 0)
    try:
        if int(open_deals) > 0:
            return False, "Excluded — Open Deal in Progress"
    except (ValueError, TypeError):
        pass
    return True, None


def check_industry(account):
    """Returns (passed: bool, reason: str)"""
    industry = (account.get("industry") or "").strip().lower()
    if industry == "":
        # Blank industry: pass through (will be assessed in research stage)
        return True, None
    if industry not in TARGET_INDUSTRIES:
        return False, "Excluded — Industry Not in ICP"
    return True, None


def check_negative_keywords(account):
    """Returns (passed: bool, reason: str)"""
    fields_to_check = [
        account.get("company_name", ""),
        account.get("industry", ""),
        account.get("domain", ""),
    ]
    combined = " ".join(f.lower() for f in fields_to_check if f)
    for keyword in NEGATIVE_KEYWORDS:
        if keyword in combined:
            return False, f"Excluded — Negative Keyword Match ({keyword})"
    return True, None


# ── Main Filter Pipeline ───────────────────────────────────────────────────────

FILTERS = [
    ("headcount", lambda a, cfg: check_headcount(a, cfg.get("headcount_min"), cfg.get("headcount_max"))),
    ("geography", lambda a, cfg: check_geography(a)),
    ("active_customer", lambda a, cfg: check_active_customer(a)),
    ("already_enriched", lambda a, cfg: check_already_enriched(a)),
    ("open_deals", lambda a, cfg: check_open_deals(a)),
    ("industry", lambda a, cfg: check_industry(a)),
    ("negative_keywords", lambda a, cfg: check_negative_keywords(a)),
]


def filter_accounts(input_data: dict) -> dict:
    accounts = input_data.get("accounts", [])
    batch_limit = input_data.get("batch_limit", 100)

    qualified = []
    excluded = []

    for account in accounts:
        exclude_reason = None

        for filter_name, filter_fn in FILTERS:
            passed, reason = filter_fn(account, input_data)
            if not passed:
                exclude_reason = reason
                break

        if exclude_reason:
            excluded.append({
                **account,
                "exclusion_reason": exclude_reason,
            })
        else:
            qualified.append(account)

        if len(qualified) >= batch_limit:
            break

    return {
        "qualified": qualified,
        "excluded": excluded,
        "summary": {
            "total_input": len(accounts),
            "qualified": len(qualified),
            "excluded": len(excluded),
        }
    }


# ── CLI Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python filter_accounts.py input.json output.json")
        sys.exit(1)

    input_path, output_path = sys.argv[1], sys.argv[2]

    with open(input_path, "r") as f:
        input_data = json.load(f)

    result = filter_accounts(input_data)

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Filter complete: {result['summary']['qualified']} qualified, {result['summary']['excluded']} excluded")
