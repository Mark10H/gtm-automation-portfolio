"""
score_accounts.py
-----------------
Composite enrichment scoring model for filtered HubSpot accounts.
Scores each account using ZoomInfo intent signals + live web research signals.
Hard-disqualifies accounts scoring <= 60.

Scoring model:
  ZoomInfo sub-score  (max 55): intent signals + scoops, weighted by recency and type
  Web research sub-score (max 45): trigger signals across 4 categories, weighted by recency

  Enrichment score = ZI sub-score + web research sub-score (1–100)
  Threshold: score > 60 required to proceed to research

Input schema (intent_data.json):
{
  "accounts": [
    {
      "company_id": "...",
      "company_name": "...",
      "zi_match_status": "matched|no_signals|not_found|domain_mismatch",
      "intent_signals": [...],    # from ZoomInfo enrich_intent
      "scoops": [...],            # from ZoomInfo enrich_scoops
      "web_research": [           # from Step 1.5B.5 live web searches
        {
          "trigger_category": "A|B|C|D",
          "signal_date": "YYYY-MM-DD",  # null if unknown
          "headline": "...",
          "source": "..."
        }
      ]
    }
  ]
}

Output schema (scored_accounts.json):
{
  "ranked": [...],           # accounts with score > 60, sorted desc
  "low_score_excluded": [...],
  "zi_excluded": [...]       # not_found or domain_mismatch accounts
}

Usage:
    python score_accounts.py intent_data.json scored_accounts.json
"""

import json
import sys
from datetime import datetime, date

# ── Scoring Weights ────────────────────────────────────────────────────────────

ZI_MAX = 55
WEB_MAX = 45
THRESHOLD = 60

# ZoomInfo intent signal weights by topic relevance
INTENT_TOPIC_WEIGHTS = {
    "Digital Transformation": 10,
    "Operations Optimization": 10,
    "Operations Management": 8,
    "Legal Operations": 8,
    "Document Management Software": 7,
    "Manufacturing operations management": 7,
    "Customer Success Software": 6,
    "SaaS Operations Management": 8,
    "Software as a Service (SaaS)": 5,
}

# Web research trigger category base points
WEB_CATEGORY_BASE = {
    "A": 10,   # News/Milestone
    "B": 15,   # Digital Transformation & AI (highest — hardest to fabricate)
    "C": 12,   # Operational Gap
    "D": 8,    # Growth/Retention Gap
}

# Recency multipliers for web research signals
def recency_multiplier(signal_date_str):
    if not signal_date_str:
        return 0.5  # Unknown date — discounted
    try:
        signal_date = datetime.strptime(signal_date_str, "%Y-%m-%d").date()
        days_ago = (date.today() - signal_date).days
        if days_ago <= 90:
            return 1.5
        elif days_ago <= 365:
            return 1.0
        else:
            return 0.3
    except ValueError:
        return 0.5


# ── ZoomInfo Scoring ───────────────────────────────────────────────────────────

def score_zi(account):
    """Compute ZoomInfo sub-score (max 55)."""
    intent_signals = account.get("intent_signals", [])
    scoops = account.get("scoops", [])

    # Intent signal score: sum topic weights, cap at 35
    intent_score = 0
    for signal in intent_signals:
        topic = signal.get("topic", "")
        weight = INTENT_TOPIC_WEIGHTS.get(topic, 3)
        score_val = signal.get("score", 60)
        # Scale by signal strength (score 60-100 → multiplier 0.8–1.2)
        strength = 0.8 + ((score_val - 60) / 40) * 0.4
        intent_score += weight * strength
    intent_score = min(intent_score, 35)

    # Scoop score: each scoop adds points based on recency, cap at 20
    scoop_score = 0
    for scoop in scoops:
        published = scoop.get("publishedDate", "")
        multiplier = recency_multiplier(published[:10] if published else None)
        scoop_score += 5 * multiplier
    scoop_score = min(scoop_score, 20)

    return min(intent_score + scoop_score, ZI_MAX)


# ── Web Research Scoring ───────────────────────────────────────────────────────

def score_web(account):
    """Compute web research sub-score (max 45)."""
    signals = account.get("web_research", [])
    if not signals:
        return 0

    # One score per category (best signal wins per category, no double-counting)
    category_scores = {}
    for signal in signals:
        cat = signal.get("trigger_category", "").upper()
        if cat not in WEB_CATEGORY_BASE:
            continue
        base = WEB_CATEGORY_BASE[cat]
        multiplier = recency_multiplier(signal.get("signal_date"))
        score = base * multiplier
        if cat not in category_scores or score > category_scores[cat]:
            category_scores[cat] = score

    total = sum(category_scores.values())
    return min(total, WEB_MAX)


# ── Main Scoring Pipeline ──────────────────────────────────────────────────────

def score_accounts(input_data: dict) -> dict:
    accounts = input_data.get("accounts", [])

    ranked = []
    low_score_excluded = []
    zi_excluded = []

    for account in accounts:
        zi_status = account.get("zi_match_status", "matched")

        # ZoomInfo identity failures — exclude before scoring
        if zi_status in ("not_found", "domain_mismatch"):
            zi_excluded.append({
                **account,
                "exclusion_reason": f"Excluded — ZoomInfo {zi_status.replace('_', ' ').title()}",
                "resolution": "Verify company domain in HubSpot matches ZoomInfo record. Correct domain or company name, then re-run.",
            })
            continue

        # M&A check — flagged in scoop pull step
        if account.get("is_ma_target"):
            zi_excluded.append({
                **account,
                "exclusion_reason": "Excluded — M&A Target (detected via ZoomInfo scoop)",
            })
            continue

        zi_score = score_zi(account)
        web_score = score_web(account)
        total_score = round(zi_score + web_score, 1)

        scored = {
            **account,
            "zi_sub_score": round(zi_score, 1),
            "web_sub_score": round(web_score, 1),
            "enrichment_score": total_score,
        }

        if total_score <= THRESHOLD:
            low_score_excluded.append({
                **scored,
                "exclusion_reason": f"Excluded — Enrichment Score Below Threshold (score: {total_score})",
            })
        else:
            ranked.append(scored)

    # Sort by enrichment score descending, take top 10
    ranked.sort(key=lambda a: a["enrichment_score"], reverse=True)
    top_10 = ranked[:10]

    return {
        "ranked": top_10,
        "low_score_excluded": low_score_excluded,
        "zi_excluded": zi_excluded,
        "summary": {
            "total_scored": len(accounts) - len(zi_excluded),
            "qualified": len(top_10),
            "low_score_excluded": len(low_score_excluded),
            "zi_excluded": len(zi_excluded),
        }
    }


# ── ZoomInfo Fallback (no ZI connection) ──────────────────────────────────────

def score_accounts_web_only(input_data: dict) -> dict:
    """
    Fallback scoring when ZoomInfo MCP is unavailable.
    Web research sub-score normalized to 100 (counts as full enrichment score).
    Threshold remains > 60.
    """
    accounts = input_data.get("accounts", [])
    ranked = []
    low_score_excluded = []

    for account in accounts:
        web_score = score_web(account)
        # Normalize to 100
        normalized = round((web_score / WEB_MAX) * 100, 1)

        scored = {
            **account,
            "zi_sub_score": None,
            "web_sub_score": round(web_score, 1),
            "enrichment_score": normalized,
            "scoring_mode": "web_only",
        }

        if normalized <= THRESHOLD:
            low_score_excluded.append({
                **scored,
                "exclusion_reason": f"Excluded — Enrichment Score Below Threshold (score: {normalized}, web-only mode)",
            })
        else:
            ranked.append(scored)

    ranked.sort(key=lambda a: a["enrichment_score"], reverse=True)
    top_10 = ranked[:10]

    return {
        "ranked": top_10,
        "low_score_excluded": low_score_excluded,
        "zi_excluded": [],
        "summary": {
            "scoring_mode": "web_only",
            "qualified": len(top_10),
            "low_score_excluded": len(low_score_excluded),
        }
    }


# ── CLI Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python score_accounts.py intent_data.json scored_accounts.json")
        sys.exit(1)

    input_path, output_path = sys.argv[1], sys.argv[2]

    with open(input_path, "r") as f:
        input_data = json.load(f)

    result = score_accounts(input_data)

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Scoring complete: {result['summary']['qualified']} qualified for research, "
          f"{result['summary']['low_score_excluded']} below threshold, "
          f"{result['summary']['zi_excluded']} ZI excluded")
