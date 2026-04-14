"""
filter_accounts.py — Moxo BDR Account Filter
=============================================
Applies all 8 SOP rules from sop-rules.md to a list of HubSpot accounts
and returns qualified accounts + a structured audit log of exclusions.

USAGE:
    python scripts/filter_accounts.py <input_json_path> [output_json_path]

    input_json_path  : path to JSON file with accounts to filter
    output_json_path : path to write filtered results (default: prints to stdout)

INPUT JSON SCHEMA:
{
  "rep_name":       "Spencer Johnson",
  "headcount_min":  50,          // integer, or null if rep said "no preference"
  "headcount_max":  500,         // integer, or null if rep said "no preference"
  "batch_limit":    50,          // max accounts to qualify for the scoring pool (default: 50; SKILL.md Step 2 overrides this to 100 to widen the pool for enrichment scoring)
  "target_batch":   5,           // final batch size — used for pool_exhausted warning (default: 5)
  "already_researched_this_session": ["Company A", "Company B"],
  "companies": [
    {
      "company_id":          "12345",
      "company":             "Acme Corp",
      "website":             "acmecorp.com",
      "industry":            "Financial Services",
      "employees":           250,        // integer or range string "201-500", or null
      "country":             "United States",
      "is_active_customer":  false,      // bool or null
      "ai_claude_enriched":  "No",       // "Yes"/"No" or null (HubSpot Yes/No dropdown)
      "has_open_deal":       false,      // bool — pre-computed from deal stage check
      "open_deal_stage":     "",         // string — e.g. "Contract Sent"
      "is_ma_target":        false,      // bool — pre-computed from research
      "ma_detail":           "",         // string — e.g. "acquisition by NationalLaw Partners"
      "company_owner":       "Spencer Johnson",
      "company_owner_b":     ""
    }
  ]
}

OUTPUT JSON SCHEMA:
{
  "qualified": [ { ...company fields + any _note fields added during filtering... } ],
  "excluded": [
    {
      "company":           "Beta Corp",
      "website":           "betacorp.com",
      "industry":          "Healthcare",
      "company_owner":     "Spencer Johnson",
      "exclusion_reason":  "Excluded — Industry Not in ICP (Healthcare)",
      "kb_alignment_note": ""
    }
  ],
  "skipped_this_session": ["Company already done"],
  "summary": {
    "total_input":      20,
    "total_qualified":  5,
    "total_excluded":   14,
    "total_skipped":    1,
    "batch_limit":      5,
    "pool_exhausted":   false,
    "exclusion_counts": { "Outside Headcount Range": 3, ... }
  }
}
"""

import sys
import json
import re
import os

# ---------------------------------------------------------------------------
# SOP Constants
# ---------------------------------------------------------------------------

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

# Industry aliases — maps common variants to in-scope categories
INDUSTRY_ALIASES = {
    "mortgage":               "financial services",
    "lending":                "financial services",
    "accounting":             "business services",
    "cpa":                    "business services",
    "saas":                   "computer software",
    "software":               "computer software",
    "consulting":             "business services",
    "professional services":  "business services",
    "investment":             "investment management",
    "wealth management":      "financial services",
    "private equity":         "investment management",
    "venture capital":        "investment management",
    "property management":    "real estate",
    "construction":           "real estate",
    "technology":             "it and services",
    "information technology": "it and services",
}

NEGATIVE_KEYWORDS = [
    "university",
    "school",
    "college",
    "non-profit",
    "nonprofit",
    "government",
    "defense",
    "weapons",
    ".edu",
    "school district",
]

IN_SCOPE_COUNTRIES = {
    "united states",
    "us",
    "usa",
    "u.s.",
    "u.s.a.",
    "canada",
    "ca",
    "puerto rico",
    "u.s. virgin islands",
    "us virgin islands",
    "north america",   # tentative — flagged in audit note
}

TENTATIVE_GEOGRAPHY = {"north america"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize(text):
    return str(text).lower().strip() if text is not None else ""


def headcount_from_raw(value):
    """
    Resolves employees field to an integer.
    - Range string "201-500" → midpoint 350
    - Plain integer or numeric string → int
    - Blank / null / "0" → None (unverified)
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "0":
        return None
    range_match = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", s)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        return (lo + hi) // 2
    try:
        v = int(s)
        return None if v == 0 else v
    except ValueError:
        return None


def resolve_industry(raw_industry):
    """
    Returns (canonical_industry, is_in_scope, alias_used).
      is_in_scope = True  → in ICP
      is_in_scope = False → not in ICP, exclude
      is_in_scope = None  → blank field, don't exclude but flag
    """
    if not raw_industry or not raw_industry.strip():
        return (raw_industry, None, None)

    norm = normalize(raw_industry)

    if norm in TARGET_INDUSTRIES:
        return (raw_industry, True, None)

    for alias_key, canonical in INDUSTRY_ALIASES.items():
        if alias_key in norm:
            return (canonical, True, alias_key)

    return (raw_industry, False, None)


def check_negative_keywords(company_name, industry, website):
    """Returns the first matched keyword, or None if clean."""
    targets = [normalize(company_name), normalize(industry), normalize(website)]
    for keyword in NEGATIVE_KEYWORDS:
        for target in targets:
            if keyword in target:
                return keyword
    return None


# ---------------------------------------------------------------------------
# Per-account filter
# ---------------------------------------------------------------------------

def filter_account(company, headcount_min, headcount_max, already_researched):
    """
    Returns:
        ("qualified", None, None)
        ("excluded", exclusion_reason_str, kb_alignment_note_or_None)
        ("skip",     reason_str,           None)
    """
    name     = company.get("company", "")
    website  = company.get("website", "") or ""
    industry = company.get("industry", "") or ""
    country  = company.get("country", "") or ""
    raw_emp  = company.get("employees")

    # In-session dedup
    if name in already_researched:
        return ("skip", f"Already researched this session — {name}", None)

    # --- Rule 1: Headcount ---
    if headcount_min is not None or headcount_max is not None:
        emp = headcount_from_raw(raw_emp)
        if emp is None:
            company["_headcount_note"] = (
                "Headcount unverified — included for research (blank or zero in HubSpot)"
            )
        else:
            too_small = headcount_min is not None and emp < headcount_min
            too_large = headcount_max is not None and emp > headcount_max
            if too_small or too_large:
                lo = headcount_min if headcount_min is not None else "any"
                hi = headcount_max if headcount_max is not None else "any"
                return (
                    "excluded",
                    f"Excluded — Outside Headcount Range ({emp} employees, rep range {lo}–{hi})",
                    None,
                )

    # --- Rule 2: Geography ---
    norm_country = normalize(country)
    if not norm_country:
        domain_lower = normalize(website)
        if domain_lower.endswith(".ca"):
            company["_geo_note"] = "Geography unverified — assumed Canada based on .ca domain"
        else:
            company["_geo_note"] = (
                "Geography unverified — assumed US based on .com domain or blank field; "
                "verify during research"
            )
    elif norm_country not in IN_SCOPE_COUNTRIES:
        return ("excluded", f"Excluded — Outside Geography (HQ in {country})", None)
    elif norm_country in TENTATIVE_GEOGRAPHY:
        company["_geo_note"] = (
            "Geography listed as 'North America' — verify specific country during research"
        )

    # --- Rule 3: Active Customer ---
    # HubSpot property "Is an Active customer?" is any of Yes → exclude
    is_customer = company.get("is_active_customer")
    if is_customer is True:
        return ("excluded", "Excluded — Active Customer", None)

    # --- Rule 4: Already Enriched ---
    enriched = company.get("ai_claude_enriched")
    if str(enriched).lower() == "yes":
        return (
            "excluded",
            "Excluded — Already Enriched (AI Claude Enriched flag = yes in HubSpot)",
            None,
        )

    # --- Rule 5: Open Deal ---
    has_open_deal = company.get("has_open_deal")
    if has_open_deal is True:
        deal_stage = company.get("open_deal_stage") or "stage unrecognized, treated as active"
        return (
            "excluded",
            f"Excluded — Open Deal in Progress ({deal_stage})",
            None,
        )

    # --- Rule 6: M&A Target ---
    is_ma = company.get("is_ma_target")
    if is_ma is True:
        ma_detail = company.get("ma_detail") or "acquisition or merger in progress"
        return ("excluded", f"Excluded — M&A Target ({ma_detail})", None)

    # --- Rule 7: Target Industries ---
    canonical_industry, is_in_scope, alias_used = resolve_industry(industry)
    if is_in_scope is False:
        return ("excluded", f"Excluded — Industry Not in ICP ({industry})", None)
    if is_in_scope is None:
        company["_industry_note"] = (
            "Industry blank in HubSpot — infer from website during research"
        )
    if alias_used:
        company["_industry_note"] = (
            f"Industry '{industry}' mapped to '{canonical_industry}' via alias — "
            "confirm during research"
        )

    # --- Rule 8: Negative Keywords ---
    matched_keyword = check_negative_keywords(name, industry, website)
    if matched_keyword:
        return (
            "excluded",
            f"Excluded — Negative Keyword Match ('{matched_keyword}' found in name/industry/domain)",
            None,
        )

    return ("qualified", None, None)


# ---------------------------------------------------------------------------
# Main filter runner
# ---------------------------------------------------------------------------

def run_filter(data):
    headcount_min = data.get("headcount_min")
    headcount_max = data.get("headcount_max")
    batch_limit   = int(data.get("batch_limit", 50))   # scoring pool size
    target_batch  = int(data.get("target_batch", 5))   # final batch target for pool_exhausted check
    already_done  = set(data.get("already_researched_this_session", []))
    companies     = data.get("companies", [])

    qualified        = []
    excluded         = []
    skipped          = []
    exclusion_counts = {}

    for company in companies:
        if len(qualified) >= batch_limit:
            break

        result, reason, note = filter_account(
            company, headcount_min, headcount_max, already_done
        )

        if result == "qualified":
            qualified.append(company)

        elif result == "excluded":
            # Extract short category label for the counts summary
            cat = (
                reason.split("—")[1].strip().split("(")[0].strip()
                if "—" in reason
                else reason
            )
            exclusion_counts[cat] = exclusion_counts.get(cat, 0) + 1
            excluded.append(
                {
                    "company":           company.get("company", ""),
                    "website":           company.get("website", ""),
                    "industry":          company.get("industry", ""),
                    "company_owner":     company.get("company_owner", ""),
                    "exclusion_reason":  reason,
                    "kb_alignment_note": note or "",
                }
            )

        elif result == "skip":
            skipped.append(company.get("company", ""))

    # pool_exhausted means we couldn't fill the final target batch (not the scoring pool)
    pool_exhausted = len(qualified) < target_batch and len(companies) > 0

    return {
        "qualified":               qualified,
        "excluded":                excluded,
        "skipped_this_session":    skipped,
        "summary": {
            "total_input":      len(companies),
            "total_qualified":  len(qualified),
            "total_excluded":   len(excluded),
            "total_skipped":    len(skipped),
            "batch_limit":      batch_limit,
            "target_batch":     target_batch,
            "pool_exhausted":   pool_exhausted,
            "exclusion_counts": exclusion_counts,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    input_path  = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(input_path):
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result       = run_filter(data)
    output_json  = json.dumps(result, indent=2, ensure_ascii=False)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"Filter complete → {output_path}", file=sys.stderr)
    else:
        print(output_json)

    _print_summary(result["summary"])
    sys.exit(0 if result["summary"]["total_qualified"] > 0 else 1)


def _print_summary(summary):
    sep = "─" * 38
    lines = [
        "",
        sep,
        " Filter Summary",
        sep,
        f"  Input      : {summary['total_input']} accounts",
        f"  Qualified  : {summary['total_qualified']}  (scoring pool limit: {summary['batch_limit']}, target batch: {summary['target_batch']})",
        f"  Excluded   : {summary['total_excluded']}",
        f"  Skipped    : {summary['total_skipped']}  (already done this session)",
    ]
    if summary["exclusion_counts"]:
        lines.append("")
        lines.append("  Exclusion breakdown:")
        for reason, count in sorted(
            summary["exclusion_counts"].items(), key=lambda x: -x[1]
        ):
            lines.append(f"    {reason}: {count}")
    if summary["pool_exhausted"]:
        lines.append("")
        lines.append(
            f"  ⚠  Pool exhausted — only {summary['total_qualified']} of "
            f"{summary['target_batch']} target accounts qualified."
        )
        lines.append(
            "     Suggest: expand headcount range or check for newly assigned accounts."
        )
    lines.append(sep)
    print("\n".join(lines), file=sys.stderr)


if __name__ == "__main__":
    main()
