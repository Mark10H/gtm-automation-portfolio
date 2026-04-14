"""
territory_health.py — Moxo BDR Territory Health View
=====================================================
Takes the full set of HubSpot company records assigned to a rep,
applies all 8 SOP filter rules, and produces a territory health
dashboard showing enrichment progress, exclusion breakdown, and
eligible pool composition.

USAGE:
    python scripts/territory_health.py <input_json_path> [output_json_path]

    input_json_path  : path to JSON file produced by Claude from HubSpot pull
    output_json_path : path to write JSON report (default: prints to stdout)

    The formatted dashboard is always printed to stderr for display in chat.

─────────────────────────────────────────────────────────────
INPUT JSON SCHEMA
─────────────────────────────────────────────────────────────
{
  "rep_name":      "Spencer Johnson",
  "as_of":         "2026-04-08",          // ISO date — defaults to today
  "batch_size":    5,                     // accounts per batch (default: 5)
  "companies": [
    {
      "company":             "Acme Corp",
      "website":             "acmecorp.com",
      "industry":            "Financial Services",
      "employees":           250,          // int, range string "201-500", or null
      "country":             "United States",
      "is_active_customer":  false,        // bool or null
      "ai_claude_enriched":  "No",         // "Yes"/"No" or null (HubSpot Yes/No dropdown, Yes = already done)
      "has_open_deal":       false,        // bool
      "open_deal_stage":     "",
      "is_ma_target":        false,
      "company_owner":       "Spencer Johnson",
      "company_owner_b":     ""
    }
  ]
}

─────────────────────────────────────────────────────────────
OUTPUT JSON SCHEMA
─────────────────────────────────────────────────────────────
{
  "rep_name":   "Spencer Johnson",
  "as_of":      "2026-04-08",
  "total_assigned": 127,
  "enrichment": {
    "enriched_count": 43,
    "enriched_pct":   33.9,
    "batches_completed": 8,
    "batches_remaining": 13,
    "batch_size": 5
  },
  "pipeline": {
    "active_customers": 12,
    "open_deals":        8
  },
  "exclusions": {
    "already_enriched":   43,
    "active_customer":    12,
    "open_deal":           8,
    "outside_geography":   7,
    "industry_not_in_icp": 6,
    "negative_keyword":    2,
    "outside_headcount":   0,
    "ma_target":           0,
    "total_excluded":     78
  },
  "eligible_pool": {
    "total": 49,
    "headcount_unverified": 8,
    "by_industry": {
      "Financial Services": 22,
      "Real Estate": 15,
      ...
    },
    "by_headcount_band": {
      "1-50":     3,
      "51-100":  18,
      "101-250": 26,
      "251-500": 12,
      "501+":     2,
      "Unknown":  8
    }
  },
  "dashboard": "... formatted ASCII text for chat display ..."
}
"""

import sys
import json
import re
import os
from datetime import date, datetime
from collections import defaultdict


# ─────────────────────────────────────────────────────────
# SOP constants (mirrors filter_accounts.py — keep in sync)
# ─────────────────────────────────────────────────────────

TARGET_INDUSTRIES = {
    "real estate", "financial services", "banking", "computer software",
    "it and services", "insurance", "manufacturing", "law practice",
    "legal services", "business services", "investment management",
    "investment banking", "commercial real estate",
}

INDUSTRY_ALIASES = {
    "mortgage":               "Financial Services",
    "lending":                "Financial Services",
    "accounting":             "Business Services",
    "cpa":                    "Business Services",
    "saas":                   "Computer Software",
    "software":               "Computer Software",
    "consulting":             "Business Services",
    "professional services":  "Business Services",
    "investment":             "Investment Management",
    "wealth management":      "Financial Services",
    "private equity":         "Investment Management",
    "venture capital":        "Investment Management",
    "property management":    "Real Estate",
    "construction":           "Real Estate",
    "technology":             "IT and Services",
    "information technology": "IT and Services",
}

NEGATIVE_KEYWORDS = [
    "university", "school", "college", "non-profit", "nonprofit",
    "government", "defense", "weapons", ".edu", "school district",
]

IN_SCOPE_COUNTRIES = {
    "united states", "us", "usa", "u.s.", "u.s.a.", "canada", "ca",
    "puerto rico", "u.s. virgin islands", "us virgin islands", "north america",
}

HEADCOUNT_BANDS = [
    (1,   50,  "1–50"),
    (51,  100, "51–100"),
    (101, 250, "101–250"),
    (251, 500, "251–500"),
    (501, None,"501+"),
]


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def normalize(text):
    return str(text).lower().strip() if text is not None else ""


def headcount_from_raw(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "0":
        return None
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", s)
    if m:
        return (int(m.group(1)) + int(m.group(2))) // 2
    try:
        v = int(s)
        return None if v == 0 else v
    except ValueError:
        return None


def headcount_band(emp):
    """Return the display label for a headcount value (or None)."""
    if emp is None:
        return "Unknown"
    for lo, hi, label in HEADCOUNT_BANDS:
        if emp >= lo and (hi is None or emp <= hi):
            return label
    return "Unknown"


def resolve_industry(raw):
    if not raw or not str(raw).strip():
        return None, None   # (canonical, in_scope)
    norm = normalize(raw)
    if norm in TARGET_INDUSTRIES:
        return raw.title(), True
    for alias, canonical in INDUSTRY_ALIASES.items():
        if alias in norm:
            return canonical, True
    return raw, False


def has_negative_keyword(name, industry, website):
    targets = [normalize(name), normalize(industry or ""), normalize(website or "")]
    for kw in NEGATIVE_KEYWORDS:
        for t in targets:
            if kw in t:
                return True
    return False


# ─────────────────────────────────────────────────────────
# Progress bar helper
# ─────────────────────────────────────────────────────────

def progress_bar(value, total, width=24):
    if total == 0:
        return "░" * width
    filled = round(width * value / total)
    return "█" * filled + "░" * (width - filled)


# ─────────────────────────────────────────────────────────
# Core analyzer
# ─────────────────────────────────────────────────────────

def analyze_territory(data: dict) -> dict:
    rep_name   = data.get("rep_name", "Unknown Rep")
    as_of      = data.get("as_of", date.today().isoformat())
    batch_size = int(data.get("batch_size", 5))
    companies  = data.get("companies", [])

    total = len(companies)

    excl = defaultdict(int)   # exclusion reason → count
    eligible = []             # companies that pass all SOP filters

    for co in companies:
        name     = co.get("company", "")
        website  = co.get("website", "") or ""
        industry = co.get("industry", "") or ""
        country  = co.get("country",  "") or ""
        raw_emp  = co.get("employees")

        # Already enriched — track separately (it's a "done" not an "excluded")
        if str(co.get("ai_claude_enriched")).lower() == "yes":
            excl["already_enriched"] += 1
            continue

        # Active customer
        if co.get("is_active_customer") is True:
            excl["active_customer"] += 1
            continue

        # Open deal
        if co.get("has_open_deal") is True:
            excl["open_deal"] += 1
            continue

        # M&A target
        if co.get("is_ma_target") is True:
            excl["ma_target"] += 1
            continue

        # Geography
        norm_country = normalize(country)
        if norm_country and norm_country not in IN_SCOPE_COUNTRIES:
            excl["outside_geography"] += 1
            continue

        # Industry
        canonical, in_scope = resolve_industry(industry)
        if in_scope is False:
            excl["industry_not_in_icp"] += 1
            continue

        # Negative keywords
        if has_negative_keyword(name, industry, website):
            excl["negative_keyword"] += 1
            continue

        # Passed all filters → eligible
        eligible.append({
            "company":   name,
            "industry":  canonical or industry,
            "employees": headcount_from_raw(raw_emp),
        })

    # ── Eligible pool breakdown ──
    industry_counts = defaultdict(int)
    band_counts     = defaultdict(int)
    headcount_unverified = 0

    for co in eligible:
        ind = co["industry"] or "Unknown Industry"
        industry_counts[ind] += 1

        emp = co["employees"]
        if emp is None:
            headcount_unverified += 1
        band_counts[headcount_band(emp)] += 1

    # Sort industry by count descending
    by_industry = dict(sorted(industry_counts.items(), key=lambda x: -x[1]))

    # Sort bands in defined order
    band_order = [b[2] for b in HEADCOUNT_BANDS] + ["Unknown"]
    by_headcount = {b: band_counts[b] for b in band_order if band_counts.get(b, 0) > 0}

    # ── Enrichment metrics ──
    enriched_count    = excl["already_enriched"]
    enriched_pct      = round(enriched_count / total * 100, 1) if total else 0
    batches_completed = enriched_count // batch_size
    eligible_count    = len(eligible)
    batches_remaining = -(-eligible_count // batch_size)  # ceiling division

    # ── Total excluded (everything that's not enriched and not eligible) ──
    total_excluded = sum(v for k, v in excl.items() if k != "already_enriched")

    return {
        "rep_name":      rep_name,
        "as_of":         as_of,
        "total_assigned": total,
        "enrichment": {
            "enriched_count":    enriched_count,
            "enriched_pct":      enriched_pct,
            "batches_completed": batches_completed,
            "batches_remaining": batches_remaining,
            "batch_size":        batch_size,
        },
        "pipeline": {
            "active_customers": excl["active_customer"],
            "open_deals":       excl["open_deal"],
        },
        "exclusions": {
            "already_enriched":   enriched_count,
            "active_customer":    excl["active_customer"],
            "open_deal":          excl["open_deal"],
            "ma_target":          excl["ma_target"],
            "outside_geography":  excl["outside_geography"],
            "industry_not_in_icp":excl["industry_not_in_icp"],
            "negative_keyword":   excl["negative_keyword"],
            "total_excluded_from_pool": total_excluded,
        },
        "eligible_pool": {
            "total":               eligible_count,
            "headcount_unverified":headcount_unverified,
            "by_industry":         by_industry,
            "by_headcount_band":   by_headcount,
        },
    }


# ─────────────────────────────────────────────────────────
# Dashboard formatter
# ─────────────────────────────────────────────────────────

def format_dashboard(r: dict) -> str:
    total   = r["total_assigned"]
    enr     = r["enrichment"]
    pool    = r["eligible_pool"]
    excl    = r["exclusions"]
    pipe    = r["pipeline"]

    not_outreach_eligible = pipe["active_customers"] + pipe["open_deals"]

    # Enrichment bar
    enr_bar  = progress_bar(enr["enriched_count"], total)
    pool_bar = progress_bar(pool["total"], total)

    W  = 56   # dashboard width
    sep = "═" * W
    div = "─" * W

    def row(label, value, pct=None, bar=None):
        pct_str = f"  ({pct}%)" if pct is not None else ""
        bar_str = f"  {bar}" if bar else ""
        return f"  {label:<28} {str(value):>6}{pct_str}{bar_str}"

    def section(title):
        return f"\n  {title}\n  {div}"

    lines = [
        "",
        sep,
        f"  Territory Health — {r['rep_name']}",
        f"  As of {r['as_of']}",
        sep,
        row("Total assigned", total),
        section("ENRICHMENT PROGRESS"),
        row("✓ Enriched (completed)",
            enr["enriched_count"],
            enr["enriched_pct"],
            enr_bar),
        row("  Remaining eligible pool",
            pool["total"],
            round(pool["total"] / total * 100, 1) if total else 0,
            pool_bar),
        row("  Not outreach-eligible",
            not_outreach_eligible,
            round(not_outreach_eligible / total * 100, 1) if total else 0),
        f"",
        f"  At {enr['batch_size']}/batch:  "
        f"Batches completed: {enr['batches_completed']}  |  "
        f"Batches remaining: ~{enr['batches_remaining']}",
        section("EXCLUSION BREAKDOWN"),
        row("  Already enriched",        excl["already_enriched"]),
        row("  Active customer",         excl["active_customer"]),
        row("  Open deal in pipeline",   excl["open_deal"]),
        row("  Outside geography",       excl["outside_geography"]),
        row("  Industry not in ICP",     excl["industry_not_in_icp"]),
        row("  Negative keyword match",  excl["negative_keyword"]),
        row("  M&A target",              excl["ma_target"]),
    ]

    if pool["total"] > 0:
        lines += [
            section(f"ELIGIBLE POOL — {pool['total']} ACCOUNTS"),
        ]

        # Industry breakdown
        lines.append("  By Industry:")
        for ind, cnt in pool["by_industry"].items():
            pct = round(cnt / pool["total"] * 100)
            lines.append(f"    {ind:<30}  {cnt:>3}  ({pct}%)")

        # Headcount breakdown
        lines.append("")
        lines.append("  By Headcount:")
        for band, cnt in pool["by_headcount_band"].items():
            lines.append(f"    {band:<30}  {cnt:>3}")

        if pool["headcount_unverified"] > 0:
            lines.append(
                f"\n  ⚠  {pool['headcount_unverified']} accounts have unverified headcount — "
                f"confirm during research."
            )
    else:
        lines.append("")
        lines.append("  ✓ Eligible pool is empty — territory fully covered or")
        lines.append("    all remaining accounts excluded by SOP filters.")
        lines.append("    Ask your manager to assign additional accounts.")

    lines.append("")
    lines.append(sep)
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

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

    result    = analyze_territory(data)
    dashboard = format_dashboard(result)
    result["dashboard"] = dashboard

    output_json = json.dumps(result, indent=2, ensure_ascii=False)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"Territory health report → {output_path}", file=sys.stderr)
    else:
        print(output_json)

    # Always print dashboard to stderr for immediate chat display
    print(dashboard, file=sys.stderr)


if __name__ == "__main__":
    main()
