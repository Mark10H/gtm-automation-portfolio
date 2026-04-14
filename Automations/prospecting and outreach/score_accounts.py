"""
score_accounts.py — Moxo BDR Enrichment Scorer
===============================================
Takes a pool of qualified accounts pre-enriched with ZoomInfo intent signals,
scoops, AND live web research signals, then computes a composite enrichment
score (1-100) that balances ZoomInfo data (which can lag) with live intent
signals found via web research. Accounts scoring below 70 are hard-disqualified.

USAGE:
    python scripts/score_accounts.py <input_json_path> [output_json_path]

    input_json_path  : JSON file produced by Claude after calling enrich_intent,
                       enrich_scoops, AND running web research per account
                       (see INPUT SCHEMA below)
    output_json_path : path to write ranked output (default: prints to stdout)

-------------------------------------------------------------
SCORING MODEL OVERVIEW
-------------------------------------------------------------

ENRICHMENT SCORE (1-100)
  = ZoomInfo Sub-score (max 55) + Web Research Sub-score (max 45)

  Accounts with enrichment score < 70 are DISQUALIFIED (low_score_excluded).
  Top 10 qualifying accounts are returned.

---- ZOOMINFO SUB-SCORE (max 55) ----

  Internally: intent_score + scoop_score (existing model, max 100 combined)
  Normalized: zi_sub = min((intent_score + scoop_score) / 100 * 55, 55)

  Intent Score (internal, max 65):
    Base topic points per matched topic:
      Tier 1 - Primary Fit (30 pts each)
        Digital Transformation, Operations Optimization, Operations Management
      Tier 2 - Strong Signal (20 pts each)
        Legal Operations, Document Management Software,
        Manufacturing operations management, Customer Success Software
      Tier 3 - Supporting Signal (10 pts each)
        SaaS Operations Management, Software as a Service (SaaS)
    Signal Score multiplier: 85-100=x1.5 | 70-84=x1.2 | 60-69=x1.0
    Raw capped at 50, then audience bonus: A=+15 | B=+10 | C=+5 | D=+2 | E=0

  Scoop Score (internal, max 35):
    Tier 1A (25 pts): Executive Move, Pain Point
    Tier 1B (20 pts): New Hire, Management Move
    Tier 2A (15 pts): Hiring Plans, Facilities Relocation/Expansion, Funding
    Tier 2B (12 pts): Project, Product Launch, Partnership
    Tier 3  ( 7 pts): Open Position, Promotion, Commentary
    Penalized: Layoffs (-10, with carve-out rules)
    M&A: flagged, not scored
    Per-scoop modifiers:
      Recency: <=30d=x1.5 | 31-90d=x1.2 | 91-180d=x1.0 | 181-365d=x0.6 | >365d=x0.3
      Department bonus: +5/scoop (max +10) for Operations, C-Suite, Legal
      Description keyword bonus: +3/keyword (max +6/scoop)

---- WEB RESEARCH SUB-SCORE (max 45) ----

  Based on trigger signals found across 4 categories during Step 1.5B.5.
  Each signal earns base points; recency multiplier is applied per signal.
  Raw points capped at 45.

  Base points per signal found:
    Category A - News/Milestone:              12 pts
    Category B - Digital Transformation & AI: 15 pts  (highest intent signal)
    Category C - Operational Gap:             13 pts
    Category D - Growth/Retention Gap:        10 pts

  Recency multiplier (based on signal_date):
    <= 90 days  -> x1.5  (last 3 months — live, high confidence)
    91-365 days -> x1.0  (3-12 months — relevant but aging)
    > 365 days  -> x0.3  (over a year — contextual only)

  Multiple signals in the same category each earn points independently (up to cap).

---- QUALIFICATION THRESHOLD ----
    Enrichment score >= 70  -> QUALIFIED, enters ranked pool
    Enrichment score <  70  -> DISQUALIFIED, goes to low_score_excluded

---- PRIORITY TIERS (for qualified accounts) ----
    Hot  (>=85): research first
    Warm (70-84): research second
    (accounts below 70 are excluded before tiering)

-------------------------------------------------------------
INPUT JSON SCHEMA
-------------------------------------------------------------
{
  "scoring_date": "2026-04-10",       // ISO date - used for recency math
  "top_n": 10,                        // how many top accounts to surface (default: 10)
  "accounts": [
    {
      "company":    "Acme Financial",
      "website":    "acmefinancial.com",
      "industry":   "Financial Services",
      "employees":  250,
      "company_owner": "Spencer Johnson",
      // ZoomInfo match status - set by Claude during Step 1.5A/1.5B.
      "zi_match_status": "matched",   // matched | no_signals | not_found | domain_mismatch
      "zi_matched_name": "",          // optional - name ZI actually matched
      // ZoomInfo enrich_intent output
      "intent_signals": [
        {
          "topic":            "Digital Transformation",
          "signalScore":      85,
          "audienceStrength": "B",
          "signalDate":       "2026-04-01"
        }
      ],
      // ZoomInfo enrich_scoops output
      "scoops": [
        {
          "scoopType":     "Executive Move",
          "department":    "C-Suite",
          "description":   "New COO appointed with mandate to modernize operations",
          "publishedDate": "2026-03-15",
          "link":          "https://example.com/press-release"
        }
      ],
      // Web research signals from Step 1.5B.5 - one entry per signal found
      "web_research": [
        {
          "trigger_category": "B",           // A | B | C | D
          "signal_date":      "2026-03-20",  // YYYY-MM-DD; use YYYY-01-01 if only year known
          "headline":         "Acme Financial announces AI-driven onboarding initiative",
          "source":           "Business Wire - https://businesswire.com/acme-ai"
        }
      ]
    }
  ]
}

-------------------------------------------------------------
OUTPUT JSON SCHEMA
-------------------------------------------------------------
{
  "ranked_accounts": [
    {
      "rank": 1,
      "company": "Acme Financial",
      "website": "acmefinancial.com",
      "industry": "Financial Services",
      "company_owner": "Spencer Johnson",
      "enrichment_score": 87,
      "zi_sub_score": 48,
      "web_sub_score": 39,
      "priority_tier": "Hot",
      "intent_score": 52,
      "scoop_score": 35,
      "web_signal_count": 3,
      "ma_flag": false,
      "zi_match_status": "matched",
      "zi_warning": null,
      "score_breakdown": {
        "zi_raw_composite": 87,
        "zi_normalized": 48,
        "web_signals_found": [
          "Category B - 21 days ago (x1.5): Acme Financial announces AI-driven onboarding"
        ],
        "web_raw_pts": 39,
        "web_pts_capped": 39,
        "intent_topics_matched": ["Digital Transformation (score 85, x1.5)"],
        "scoops_matched": ["Executive Move - C-Suite - 26 days ago (x1.5, +dept bonus)"],
        "layoff_penalty": 0
      },
      "rep_summary": "Hot (87) | ZI: 48/55 | Web: 39/45 | Digital Transformation intent + Executive Move + 2 web signals"
    }
  ],
  "low_score_excluded": [
    {
      "company": "Beta Corp",
      "enrichment_score": 42,
      "zi_sub_score": 20,
      "web_sub_score": 22,
      "exclusion_reason": "Excluded - Enrichment Score Below Threshold (score: 42)"
    }
  ],
  "ma_flagged": ["LexGroup Law"],
  "zi_excluded": [
    {
      "company": "Torres Legal Services",
      "zi_match_status": "not_found",
      "zi_warning": "ZoomInfo could not find this company..."
    }
  ],
  "not_top_n": [],
  "summary": {
    "scoring_date": "2026-04-10",
    "total_input": 30,
    "total_scored": 28,
    "qualified_count": 15,
    "disqualified_count": 13,
    "top_n_returned": 10,
    "hot": 4,
    "warm": 6,
    "ma_flagged_count": 1,
    "zi_excluded_count": 2
  }
}
"""

import sys
import json
import os
from datetime import date, datetime


# -------------------------------------------------------------
# Scoring constants
# -------------------------------------------------------------

INTENT_TOPIC_WEIGHTS = {
    # Tier 1 - Primary Fit
    "Digital Transformation":              30,
    "Operations Optimization":             30,
    "Operations Management":               30,
    # Tier 2 - Strong Signal
    "Legal Operations":                    20,
    "Document Management Software":        20,
    "Manufacturing operations management": 20,
    "Customer Success Software":           20,
    # Tier 3 - Supporting Signal
    "SaaS Operations Management":          10,
    "Software as a Service (SaaS)":        10,
}

SIGNAL_SCORE_MULTIPLIERS = [
    (85, 1.5),
    (70, 1.2),
    (60, 1.0),
]

AUDIENCE_STRENGTH_BONUS = {
    "A": 15, "B": 10, "C": 5, "D": 2, "E": 0,
}
AUDIENCE_STRENGTH_ORDER = ["A", "B", "C", "D", "E"]

SCOOP_TYPE_WEIGHTS = {
    "Executive Move":                    25,
    "Pain Point":                        25,
    "New Hire":                          20,
    "Management Move":                   20,
    "Hiring Plans":                      15,
    "Facilities Relocation / Expansion": 15,
    "Funding":                           15,
    "Project":                           12,
    "Product Launch":                    12,
    "Partnership":                       12,
    "Open Position":                     7,
    "Promotion":                         7,
    "Commentary":                        7,
    "Layoffs":                           -10,
    "Mergers & Acquisitions (M&A)":      0,
    "Earnings":                          0,
    "Initial Public Offering (IPO)":     3,
    "Divestiture":                       0,
    "Award":                             3,
    "Event":                             2,
    "Lateral Move":                      3,
    "Left Company":                      0,
    "Person-Based":                      2,
}

DEPT_BONUS_QUALIFYING = {"Operations", "C-Suite", "Legal"}
DEPT_BONUS_PTS = 5
DEPT_BONUS_CAP = 10

LAYOFF_CARVEOUT_HIRING_TYPES = {"Hiring Plans", "New Hire", "Open Position"}
LAYOFF_CARVEOUT_DEPTS        = {"Operations", "C-Suite", "Legal", "Information Technology"}
LAYOFF_PARTIAL_CARVEOUT_KEYWORDS = [
    "restructur", "realign", "refocus", "streamlin",
    "efficiency", "invest in technology", "operational", "automat",
]
LAYOFF_NEGATION_GUARDS = {"no", "not", "without", "despite", "unrelated", "non"}
LAYOFF_FULL_PENALTY    = -10
LAYOFF_PARTIAL_PENALTY = -5

DESCRIPTION_KEYWORDS = [
    "operations", "onboard", "compliance", "automation", "digital",
    "transform", "process", "workflow", "efficiency", "scale",
]
DESC_KEYWORD_BONUS_PER_MATCH   = 3
DESC_KEYWORD_BONUS_CAP_PER_SCOOP = 6

MAX_INTENT_RAW = 50
MAX_SCOOP_RAW  = 35

# New enrichment model weights
ZI_WEIGHT        = 0.55   # ZoomInfo sub-score: max 55 pts
WEB_WEIGHT       = 0.45   # Web research sub-score: max 45 pts
ENRICHMENT_THRESHOLD = 70 # Hard disqualification below this

# Web research: base points per trigger category
WEB_CATEGORY_POINTS = {
    "A": 12,  # News/Milestone
    "B": 15,  # Digital Transformation & AI (highest intent signal)
    "C": 13,  # Operational Gap
    "D": 10,  # Growth/Retention Gap
}
MAX_WEB_RAW = 45


# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------

def signal_score_multiplier(score: int) -> float:
    for threshold, mult in SIGNAL_SCORE_MULTIPLIERS:
        if score >= threshold:
            return mult
    return 1.0


def best_audience_strength(signals: list) -> str:
    found = set(s.get("audienceStrength", "E") for s in signals if s.get("audienceStrength"))
    for grade in AUDIENCE_STRENGTH_ORDER:
        if grade in found:
            return grade
    return "E"


def days_ago(date_str: str, scoring_date: date) -> int:
    if not date_str:
        return 9999
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return max(0, (scoring_date - d).days)
    except ValueError:
        return 9999


def recency_multiplier_scoop(days: int) -> float:
    """Recency multiplier for ZoomInfo scoops (finer-grained)."""
    if days <= 30:   return 1.5
    if days <= 90:   return 1.2
    if days <= 180:  return 1.0
    if days <= 365:  return 0.6
    return 0.3


def recency_multiplier_web(days: int) -> float:
    """Recency multiplier for web research signals (3-bucket)."""
    if days <= 90:   return 1.5   # last 3 months - live, high confidence
    if days <= 365:  return 1.0   # 3-12 months - relevant but aging
    return 0.3                    # over a year - contextual only


def priority_tier(score: int) -> tuple:
    if score >= 85:  return "Hot",  "Hot"
    if score >= 70:  return "Warm", "Warm"
    return "Below Threshold", "Below"


# -------------------------------------------------------------
# Per-account scorers
# -------------------------------------------------------------

def score_intent(signals: list) -> dict:
    raw_pts = 0.0
    matched = []

    for sig in signals:
        topic = sig.get("topic", "")
        base  = INTENT_TOPIC_WEIGHTS.get(topic, 0)
        if base == 0:
            continue
        sscore = sig.get("signalScore", 60)
        mult   = signal_score_multiplier(sscore)
        pts    = base * mult
        raw_pts += pts
        matched.append(f"{topic} (score {sscore}, x{mult})")

    best_aud  = best_audience_strength(signals)
    aud_bonus = AUDIENCE_STRENGTH_BONUS.get(best_aud, 0)
    capped    = min(raw_pts, MAX_INTENT_RAW)
    final     = capped + aud_bonus

    return {
        "intent_score":      round(final),
        "intent_raw_pts":    round(raw_pts, 1),
        "intent_pts_capped": round(capped, 1),
        "audience_strength": best_aud,
        "audience_bonus":    aud_bonus,
        "topics_matched":    matched,
    }


def _has_positive_keyword(text: str, keywords: list) -> bool:
    tokens = text.split()
    for i, token in enumerate(tokens):
        for kw in keywords:
            if kw in token:
                window = tokens[max(0, i - 4): i]
                if any(neg in window for neg in LAYOFF_NEGATION_GUARDS):
                    continue
                return True
    return False


def score_scoops(scoops: list, scoring_date: date) -> dict:
    raw_pts          = 0.0
    dept_bonus_total = 0
    matched          = []
    layoff_penalty   = 0
    layoff_carveout  = None
    ma_flag          = False

    has_layoff = any(s.get("scoopType") == "Layoffs" for s in scoops)

    if has_layoff:
        ops_hiring_present = any(
            s.get("scoopType") in LAYOFF_CARVEOUT_HIRING_TYPES
            and s.get("department") in LAYOFF_CARVEOUT_DEPTS
            for s in scoops
        )
        if ops_hiring_present:
            layoff_carveout = "full"
        else:
            layoff_descs = " ".join(
                (s.get("description", "") or "").lower()
                for s in scoops if s.get("scoopType") == "Layoffs"
            )
            restructure_signal = _has_positive_keyword(layoff_descs, LAYOFF_PARTIAL_CARVEOUT_KEYWORDS)
            layoff_carveout = "partial" if restructure_signal else None

    for scoop in scoops:
        stype = scoop.get("scoopType", "")
        dept  = scoop.get("department", "")
        desc  = (scoop.get("description", "") or "").lower()
        pub   = scoop.get("publishedDate", "")

        if stype == "Mergers & Acquisitions (M&A)":
            ma_flag = True
            continue

        base = SCOOP_TYPE_WEIGHTS.get(stype)
        if base is None or base == 0:
            continue

        if stype == "Layoffs":
            if layoff_carveout == "full":
                matched.append("Layoffs - carve-out applied (ops hiring detected, penalty waived)")
            elif layoff_carveout == "partial":
                penalty = abs(LAYOFF_PARTIAL_PENALTY)
                layoff_penalty += penalty
                raw_pts -= penalty
                matched.append(f"Layoffs - partial carve-out (restructuring language, -{penalty} pts)")
            else:
                penalty = abs(LAYOFF_FULL_PENALTY)
                layoff_penalty += penalty
                raw_pts -= penalty
                matched.append(f"Layoffs - full penalty (no mitigating signal, -{penalty} pts)")
            continue

        days  = days_ago(pub, scoring_date)
        rmult = recency_multiplier_scoop(days)
        pts   = base * rmult

        dept_bonus = 0
        if dept in DEPT_BONUS_QUALIFYING and dept_bonus_total < DEPT_BONUS_CAP:
            dept_bonus = min(DEPT_BONUS_PTS, DEPT_BONUS_CAP - dept_bonus_total)
            dept_bonus_total += dept_bonus

        kw_matches = sum(1 for kw in DESCRIPTION_KEYWORDS if kw in desc)
        kw_bonus   = min(kw_matches * DESC_KEYWORD_BONUS_PER_MATCH, DESC_KEYWORD_BONUS_CAP_PER_SCOOP)

        total_pts = pts + dept_bonus + kw_bonus
        raw_pts  += total_pts

        note = f"{stype}"
        if dept:   note += f" - {dept}"
        note += f" - {days}d ago (x{rmult}"
        if dept_bonus: note += f", +{dept_bonus} dept"
        if kw_bonus:   note += f", +{kw_bonus} kw"
        note += ")"
        matched.append(note)

    capped = min(max(raw_pts, 0), MAX_SCOOP_RAW)

    return {
        "scoop_score":      round(capped),
        "scoop_raw_pts":    round(raw_pts, 1),
        "scoop_pts_capped": round(capped, 1),
        "layoff_penalty":   layoff_penalty,
        "layoff_carveout":  layoff_carveout,
        "ma_flag":          ma_flag,
        "scoops_matched":   matched,
    }


def score_web_research(web_signals: list, scoring_date: date) -> dict:
    """
    Score company-level web research signals from Step 1.5B.5.
    Points per signal based on trigger category, multiplied by recency.
    Raw total capped at MAX_WEB_RAW (45).
    """
    raw_pts = 0.0
    matched = []

    for signal in web_signals:
        category     = signal.get("trigger_category", "").upper().strip()
        signal_date  = signal.get("signal_date", "")
        headline     = signal.get("headline", "")

        base = WEB_CATEGORY_POINTS.get(category, 0)
        if base == 0:
            continue  # unknown category - skip

        days  = days_ago(signal_date, scoring_date)
        rmult = recency_multiplier_web(days)
        pts   = base * rmult
        raw_pts += pts

        cat_labels = {
            "A": "News/Milestone",
            "B": "Digital Transformation & AI",
            "C": "Operational Gap",
            "D": "Growth/Retention Gap",
        }
        label = cat_labels.get(category, category)
        matched.append(f"Category {category} ({label}) - {days}d ago (x{rmult}): {headline[:80]}")

    capped = min(raw_pts, MAX_WEB_RAW)

    return {
        "web_sub_score":    round(capped),
        "web_raw_pts":      round(raw_pts, 1),
        "web_pts_capped":   round(capped, 1),
        "web_signal_count": len(web_signals),
        "web_signals_found": matched,
    }


# -------------------------------------------------------------
# Rep-facing summary line
# -------------------------------------------------------------

def build_rep_summary(enrichment_score: int, tier: str,
                      zi_sub: int, web_sub: int,
                      intent_r: dict, scoop_r: dict, web_r: dict,
                      zi_status: str = "matched") -> str:
    parts = [f"{tier} ({enrichment_score}) | ZI: {zi_sub}/55 | Web: {web_sub}/45"]

    if zi_status == "not_found":
        parts.append("ZoomInfo: company not found - score is web-only")
        return " | ".join(parts)
    if zi_status == "domain_mismatch":
        parts.append("ZoomInfo: name mismatch - verify entity")

    if intent_r["topics_matched"]:
        parts.append(f"Intent: {intent_r['topics_matched'][0]}")

    if scoop_r["scoops_matched"]:
        top_scoop = next(
            (s for s in scoop_r["scoops_matched"] if not s.startswith("Layoffs")),
            scoop_r["scoops_matched"][0],
        )
        parts.append(f"Scoop: {top_scoop}")

    if web_r["web_signal_count"]:
        parts.append(f"{web_r['web_signal_count']} web signal(s)")

    if scoop_r["ma_flag"]:
        parts.append("M&A activity detected - verify SOP Rule 6")

    return " | ".join(parts)


# -------------------------------------------------------------
# ZI match status resolver
# -------------------------------------------------------------

def _resolve_zi_match_status(acct: dict) -> tuple:
    signals    = acct.get("intent_signals", [])
    scoops     = acct.get("scoops", [])
    raw_status = acct.get("zi_match_status", "").strip().lower()
    zi_matched = acct.get("zi_matched_name", "").strip()
    company    = acct.get("company", "Unknown")
    website    = acct.get("website", "")

    if not raw_status:
        status = "matched" if (signals or scoops) else "no_signals"
    else:
        status = raw_status

    warning = None

    if status == "not_found":
        hint = f"domain '{website}'" if website else "company name"
        warning = (
            f"ZoomInfo could not match '{company}' by name or {hint}. "
            f"Score uses web research only. Try: (1) check alternate name in ZI, "
            f"(2) search by domain only, (3) mark as no_signals if company is too small."
        )
    elif status == "domain_mismatch":
        matched_note = f" (ZI returned '{zi_matched}')" if zi_matched else ""
        warning = (
            f"ZoomInfo matched a different company name than HubSpot{matched_note}. "
            f"Verify before trusting ZI score. If correct, update zi_match_status to 'matched'."
        )

    return status, warning


# -------------------------------------------------------------
# Main scorer
# -------------------------------------------------------------

def score_all(data: dict) -> dict:
    raw_date = data.get("scoring_date", date.today().isoformat())
    try:
        scoring_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
    except ValueError:
        scoring_date = date.today()

    top_n    = int(data.get("top_n", 10))
    accounts = data.get("accounts", [])

    scored              = []
    ma_flagged          = []
    zi_excluded         = []
    low_score_excluded  = []

    for acct in accounts:
        signals      = acct.get("intent_signals", [])
        scoops       = acct.get("scoops", [])
        web_signals  = acct.get("web_research", [])

        zi_status, zi_warning = _resolve_zi_match_status(acct)

        # not_found and domain_mismatch: excluded from research pool entirely
        if zi_status in ("not_found", "domain_mismatch"):
            zi_excluded.append({
                "company":         acct.get("company", ""),
                "website":         acct.get("website", ""),
                "industry":        acct.get("industry", ""),
                "company_owner":   acct.get("company_owner", ""),
                "zi_match_status": zi_status,
                "zi_warning":      zi_warning,
            })
            continue

        intent_r = score_intent(signals)
        scoop_r  = score_scoops(scoops, scoring_date)
        web_r    = score_web_research(web_signals, scoring_date)

        # ZoomInfo sub-score: normalize (intent + scoop) out of 100 → max 55
        zi_raw_composite = intent_r["intent_score"] + scoop_r["scoop_score"]
        zi_sub = round(min(zi_raw_composite / 100.0 * 55, 55))

        # Web research sub-score: already capped at 45
        web_sub = web_r["web_sub_score"]

        # Final enrichment score
        # zi_unavailable: set to true in the input JSON by SKILL.md Step 1.5E when ZoomInfo
        # MCP is down. Normalizes the web-only sub-score (max 45) to a 100-point scale so
        # the 70-point threshold remains meaningful. Without this, all web-only accounts
        # would max out at 45 and be hard-disqualified even with strong live signals.
        zi_unavailable = bool(data.get("zi_unavailable", False))
        if zi_unavailable:
            fallback_score   = round(web_sub / 45 * 100) if web_sub > 0 else 0
            enrichment_score = fallback_score
        else:
            enrichment_score = zi_sub + web_sub

        if scoop_r["ma_flag"]:
            ma_flagged.append(acct.get("company", "Unknown"))

        tier, _ = priority_tier(enrichment_score)

        record = {
            "company":          acct.get("company", ""),
            "website":          acct.get("website", ""),
            "industry":         acct.get("industry", ""),
            "employees":        acct.get("employees"),
            "company_owner":    acct.get("company_owner", ""),
            "enrichment_score": enrichment_score,
            "zi_sub_score":     zi_sub,
            "web_sub_score":    web_sub,
            "priority_tier":    tier,
            "intent_score":     intent_r["intent_score"],
            "scoop_score":      scoop_r["scoop_score"],
            "web_signal_count": web_r["web_signal_count"],
            "ma_flag":          scoop_r["ma_flag"],
            "zi_match_status":  zi_status,
            "zi_warning":       zi_warning,
            "score_breakdown": {
                "zi_raw_composite":      zi_raw_composite,
                "zi_normalized":         zi_sub,
                "web_signals_found":     web_r["web_signals_found"],
                "web_raw_pts":           web_r["web_raw_pts"],
                "web_pts_capped":        web_r["web_pts_capped"],
                "intent_topics_matched": intent_r["topics_matched"],
                "intent_raw_pts":        intent_r["intent_raw_pts"],
                "intent_pts_capped":     intent_r["intent_pts_capped"],
                "audience_strength":     intent_r["audience_strength"],
                "audience_bonus":        intent_r["audience_bonus"],
                "scoops_matched":        scoop_r["scoops_matched"],
                "scoop_raw_pts":         scoop_r["scoop_raw_pts"],
                "scoop_pts_capped":      scoop_r["scoop_pts_capped"],
                "layoff_penalty":        scoop_r["layoff_penalty"],
                "layoff_carveout":       scoop_r["layoff_carveout"],
            },
            "rep_summary": build_rep_summary(
                enrichment_score, tier, zi_sub, web_sub,
                intent_r, scoop_r, web_r, zi_status
            ),
        }

        # Hard disqualification below threshold
        if enrichment_score < ENRICHMENT_THRESHOLD:
            low_score_excluded.append({
                "company":         record["company"],
                "website":         record["website"],
                "industry":        record["industry"],
                "company_owner":   record["company_owner"],
                "enrichment_score": enrichment_score,
                "zi_sub_score":    zi_sub,
                "web_sub_score":   web_sub,
                "exclusion_reason": f"Excluded - Enrichment Score Below Threshold (score: {enrichment_score})",
            })
            continue

        scored.append(record)

    # Sort descending by enrichment score, then alphabetically for ties
    scored.sort(key=lambda x: (-x["enrichment_score"], x["company"]))

    for i, acct in enumerate(scored, start=1):
        acct["rank"] = i

    top  = scored[:top_n]
    rest = scored[top_n:]

    tier_counts = {"Hot": 0, "Warm": 0}
    for acct in scored:
        t = acct["priority_tier"]
        tier_counts[t] = tier_counts.get(t, 0) + 1

    return {
        "ranked_accounts":   top,
        "not_top_n": [
            {k: v for k, v in a.items()
             if k in ("rank", "company", "enrichment_score", "zi_sub_score",
                      "web_sub_score", "priority_tier", "rep_summary", "zi_match_status")}
            for a in rest
        ],
        "low_score_excluded": low_score_excluded,
        "ma_flagged":         ma_flagged,
        "zi_excluded":        zi_excluded,
        "summary": {
            "scoring_date":       raw_date,
            "total_input":        len(accounts),
            "total_scored":       len(scored) + len(low_score_excluded),
            "qualified_count":    len(scored),
            "disqualified_count": len(low_score_excluded),
            "top_n_returned":     len(top),
            **tier_counts,
            "ma_flagged_count":   len(ma_flagged),
            "zi_excluded_count":  len(zi_excluded),
        },
    }


# -------------------------------------------------------------
# CLI
# -------------------------------------------------------------

def _print_summary(result: dict):
    s   = result["summary"]
    sep = "-" * 56
    lines = [
        "",
        sep,
        f" Enrichment Scoring - {s['scoring_date']}",
        sep,
        f"  Accounts scored    : {s['total_scored']}",
        f"  Qualified (>=70)   : {s['qualified_count']}",
        f"  Disqualified (<70) : {s['disqualified_count']}",
        f"  Top {s['top_n_returned']} selected",
        "",
        f"  Hot  (>=85) : {s.get('Hot', 0)}",
        f"  Warm (70-84): {s.get('Warm', 0)}",
    ]
    if s["ma_flagged_count"]:
        lines.append(f"\n  M&A flagged  : {s['ma_flagged_count']} - verify SOP Rule 6")
        lines.append(f"  {result['ma_flagged']}")
    if s.get("zi_excluded_count", 0):
        lines.append(f"\n  ZoomInfo excluded (not researched) : {s['zi_excluded_count']}")
        for u in result.get("zi_excluded", []):
            status_label = {
                "not_found":       "NOT FOUND in ZI",
                "domain_mismatch": "NAME MISMATCH in ZI",
            }.get(u["zi_match_status"], u["zi_match_status"].upper())
            lines.append(f"     [{status_label}]  {u['company']}  ({u['website']})")
        lines.append("     Resolve in ZoomInfo, then rerun to include these accounts.")
    if result.get("low_score_excluded"):
        lines.append(f"\n  Score-disqualified (<70) : {len(result['low_score_excluded'])}")
        for u in result["low_score_excluded"]:
            lines.append(f"     {u['company']} - score {u['enrichment_score']} (ZI:{u['zi_sub_score']}/Web:{u['web_sub_score']})")
    lines.append("")
    lines.append("  Top accounts:")
    for acct in result["ranked_accounts"]:
        lines.append(f"    #{acct['rank']:>2}  {acct['rep_summary']}")
    lines.append(sep)
    print("\n".join(lines), file=sys.stderr)


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

    result      = score_all(data)
    output_json = json.dumps(result, indent=2, ensure_ascii=False)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output_json)
        print(f"Scoring complete -> {output_path}", file=sys.stderr)
    else:
        print(output_json)

    _print_summary(result)
    sys.exit(0)


if __name__ == "__main__":
    main()
