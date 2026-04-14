"""
contact_enrichment.py — Moxo BDR Contact Data Enrichment Processor
====================================================================
Processes raw ZoomInfo contact results after Step 5 (contacts pull).

Does three things:
  1. Geography gate  — drops contacts outside US / Canada
  2. Completeness triage — classifies each contact by what data is present
  3. Enrichment queue — builds a prioritized list of enrichment tasks
     with the correct strategy (ZoomInfo re-enrich vs. web search fallback)

The script does NOT call any APIs itself. It outputs an enrichment_queue
that Claude works through using mcp enrich_contacts and WebSearch.

─────────────────────────────────────────────────────────
COMPLETENESS TIERS (priority order for outreach):

  FULL         — email AND (phone OR mobile)        ← use immediately
  EMAIL_ONLY   — email, no phone/mobile             ← enrich phone
  PHONE_ONLY   — phone OR mobile, no email          ← enrich email (harder path)
  SHELL        — neither email nor phone             ← enrich or replace
  GEO_EXCLUDED — outside US/Canada                  ← drop entirely

─────────────────────────────────────────────────────────
ENRICHMENT STRATEGIES (in attempt order for PHONE_ONLY):

  email_pattern_inference — infer email from company pattern on known contacts
                            (e.g. jsmith@ → blee@ for Bob Lee at same company)
                            Uses ZI-engage-validated reference emails when available.
                            Candidate is passed back to enrich_contacts for validation
                            before it is ever used for sending.
  zoominfo_enrich  — call enrich_contacts MCP with known fields to fill gaps
  web_search       — structured web search when ZI returns nothing
  linkedin_only    — no email findable; use LinkedIn for direct outreach instead
  replace_contact  — shell with no path; find a different contact at the company

─────────────────────────────────────────────────────────
INPUT JSON (write to [session_dir]/contacts_raw.json):
{
  "accounts": [
    {
      "company":  "Acme Financial",
      "contacts": [
        {
          "name":     "Jane Smith",
          "title":    "COO",
          "email":    "jsmith@acmefinancial.com",
          "phone":    null,
          "mobile":   "+1-415-555-0198",
          "linkedin": "https://linkedin.com/in/janesmith",
          "country":  "United States",
          "state":    "CA",
          "city":     "San Francisco"
        }
      ]
    }
  ]
}

OUTPUT JSON (stdout):
{
  "summary": {
    "total_contacts":     15,
    "geo_excluded":        2,
    "us_canada_total":    13,
    "full":                5,
    "email_only":          4,
    "phone_only":          2,
    "shell":               2,
    "enrichment_needed":   8,
    "ready_for_outreach":  5
  },
  "accounts": [
    {
      "company":       "Acme Financial",
      "contacts_full": [ ... ],
      "contacts_email_only": [ ... ],
      "contacts_phone_only": [ ... ],
      "contacts_shell":      [ ... ],
      "contacts_geo_excluded": [ ... ],
      "recommended_primary": { ... }   // best contact to lead outreach
    }
  ],
  "enrichment_queue": [
    {
      "priority":          1,
      "company":           "Acme Financial",
      "name":              "Bob Lee",
      "title":             "VP Operations",
      "gap":               "email",
      "has_email":         false,
      "has_phone":         true,
      "phone":             "+1-312-555-0177",
      "linkedin":          "https://linkedin.com/in/boblee",
      "strategy":          "zoominfo_enrich",
      "zoominfo_hint":     "enrich_contacts with name=Bob Lee, company=Acme Financial, phone=+1-312-555-0177",
      "web_search_query":  "\"Bob Lee\" \"Acme Financial\" email contact",
      "fallback_strategy": "linkedin_only"
    }
  ]
}

USAGE:
  python scripts/contact_enrichment.py <contacts_raw_json_path>
  python scripts/contact_enrichment.py <contacts_raw_json_path> --quiet
"""

import sys
import json
import os
import re

# ─────────────────────────────────────────────────────────────────────────────
# Geography: accepted countries and their state/province variants
# ─────────────────────────────────────────────────────────────────────────────

US_COUNTRY_TOKENS = {
    "united states", "united states of america",
    "us", "usa", "u.s.", "u.s.a.", "u.s.a",
}

CA_COUNTRY_TOKENS = {
    "canada", "ca",
}

ACCEPTED_COUNTRIES = US_COUNTRY_TOKENS | CA_COUNTRY_TOKENS

# Canadian provinces (ISO codes + full names)
CA_PROVINCES = {
    "ab", "alberta",
    "bc", "british columbia",
    "mb", "manitoba",
    "nb", "new brunswick",
    "nl", "newfoundland", "newfoundland and labrador",
    "ns", "nova scotia",
    "nt", "northwest territories",
    "nu", "nunavut",
    "on", "ontario",
    "pe", "prince edward island",
    "qc", "quebec",
    "sk", "saskatchewan",
    "yt", "yukon",
}

# US state codes (2-letter)
US_STATES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga",
    "hi","id","il","in","ia","ks","ky","la","me","md",
    "ma","mi","mn","ms","mo","mt","ne","nv","nh","nj",
    "nm","ny","nc","nd","oh","ok","or","pa","ri","sc",
    "sd","tn","tx","ut","vt","va","wa","wv","wi","wy",
    "dc","pr","vi","gu","mp","as",
}


def is_us_canada(contact: dict) -> tuple[bool, str]:
    """
    Returns (is_accepted, region_label).
    region_label: "US" | "Canada" | "Unknown — assumed US/CA" | "Excluded"

    Logic:
      1. Check `country` field (authoritative if present)
      2. Fall back to `state` field — CA province codes → Canada, US codes → US
      3. If no geography at all → treat as unknown, include with warning
    """
    country_raw = (contact.get("country") or "").strip().lower()
    state_raw   = (contact.get("state")   or "").strip().lower()
    city_raw    = (contact.get("city")    or "").strip().lower()

    # Step 1: explicit country
    if country_raw:
        if country_raw in US_COUNTRY_TOKENS:
            return True, "US"
        if country_raw in CA_COUNTRY_TOKENS:
            return True, "Canada"
        return False, f"Excluded ({contact.get('country', '')})"

    # Step 2: state/province fallback
    if state_raw:
        if state_raw in CA_PROVINCES:
            return True, "Canada (inferred from province)"
        if state_raw in US_STATES:
            return True, "US (inferred from state)"
        # Unrecognised state token — check if it looks like a US postal code area
        # e.g. "California", "New York"
        return False, f"Excluded (unrecognised state: {contact.get('state', '')})"

    # Step 3: no geography — include but flag
    return True, "Unknown — no geography data, included by default"


# ─────────────────────────────────────────────────────────────────────────────
# Contact completeness
# ─────────────────────────────────────────────────────────────────────────────

def has_email(contact: dict) -> bool:
    v = (contact.get("email") or "").strip()
    return bool(v) and "@" in v


def has_phone(contact: dict) -> bool:
    phone  = (contact.get("phone")  or "").strip()
    mobile = (contact.get("mobile") or "").strip()
    # Accept any string that has at least 7 digits
    for val in (phone, mobile):
        if val and len(re.sub(r"\D", "", val)) >= 7:
            return True
    return False


def best_phone(contact: dict) -> str:
    """Return mobile if available, else phone, else empty string."""
    mobile = (contact.get("mobile") or "").strip()
    phone  = (contact.get("phone")  or "").strip()
    if mobile and len(re.sub(r"\D", "", mobile)) >= 7:
        return mobile
    if phone  and len(re.sub(r"\D", "", phone))  >= 7:
        return phone
    return ""


TIER_FULL        = "FULL"
TIER_EMAIL_ONLY  = "EMAIL_ONLY"
TIER_PHONE_ONLY  = "PHONE_ONLY"
TIER_SHELL       = "SHELL"
TIER_GEO         = "GEO_EXCLUDED"

TIER_PRIORITY = {TIER_FULL: 1, TIER_EMAIL_ONLY: 2, TIER_PHONE_ONLY: 3, TIER_SHELL: 4}


def classify(contact: dict) -> str:
    e = has_email(contact)
    p = has_phone(contact)
    if e and p:
        return TIER_FULL
    if e and not p:
        return TIER_EMAIL_ONLY
    if not e and p:
        return TIER_PHONE_ONLY
    return TIER_SHELL


# ─────────────────────────────────────────────────────────────────────────────
# Email pattern inference engine
# ─────────────────────────────────────────────────────────────────────────────

# All supported local-part patterns, ordered by prevalence in corporate email
# Each entry: (pattern_key, formatter(first, last) → local_part)
EMAIL_PATTERNS: list[tuple[str, object]] = [
    ("first.last",  lambda f, l: f"{f}.{l}"    if f and l else ""),
    ("flast",       lambda f, l: f"{f[0]}{l}"  if f and l else ""),
    ("f.last",      lambda f, l: f"{f[0]}.{l}" if f and l else ""),
    ("first_last",  lambda f, l: f"{f}_{l}"    if f and l else ""),
    ("firstlast",   lambda f, l: f"{f}{l}"     if f and l else ""),
    ("first",       lambda f, l: f"{f}"        if f       else ""),
    ("last.first",  lambda f, l: f"{l}.{f}"    if f and l else ""),
    ("last_first",  lambda f, l: f"{l}_{f}"    if f and l else ""),
    ("lastfirst",   lambda f, l: f"{l}{f}"     if f and l else ""),
    ("lastf",       lambda f, l: f"{l}{f[0]}"  if f and l else ""),
    ("last",        lambda f, l: f"{l}"        if l       else ""),
]


def _name_parts(name: str) -> tuple[str, str]:
    """
    Parse 'Jane Smith', 'Mary-Anne O'Brien', 'Dr. Bob Lee Jr.' etc.
    Returns (first, last) lowercased, alpha-only, no empty strings.
    Uses first token as first name, last token as last name.
    """
    clean = re.sub(r"[^a-z\s]", "", name.lower()).split()
    # Strip common honorifics/suffixes that might be left after cleaning
    noise = {"dr", "mr", "mrs", "ms", "jr", "sr", "ii", "iii", "iv", "phd", "md"}
    clean = [t for t in clean if t not in noise]
    if not clean:
        return ("", "")
    if len(clean) == 1:
        return (clean[0], "")
    return (clean[0], clean[-1])


def _extract_domain(email: str) -> str:
    """'jsmith@acmefinancial.com' → 'acmefinancial.com'"""
    at = email.rfind("@")
    return email[at + 1:].lower().strip() if at >= 0 else ""


def _local_part(email: str) -> str:
    """'jsmith@acmefinancial.com' → 'jsmith'"""
    at = email.find("@")
    return email[:at].lower().strip() if at >= 0 else email.lower().strip()


def detect_email_pattern(
    reference_contacts: list[dict],
) -> dict | None:
    """
    Given a list of reference contacts that have both a name AND a known email,
    determine the company's email format pattern.

    Each reference contact dict:
      { "name": "Jane Smith", "email": "jsmith@acmefinancial.com",
        "zi_engage": True }   ← zi_engage = ZoomInfo validated/engage flag

    Returns:
      {
        "pattern":         "flast",
        "domain":          "acmefinancial.com",
        "confidence":      "high" | "medium" | "low",
        "match_count":     2,
        "engage_verified": True,   ← at least one ref was ZI-engage validated
        "references": [
          { "name": "Jane Smith", "email": "jsmith@acmefinancial.com",
            "zi_engage": True, "matched_pattern": "flast" }
        ]
      }
    Returns None if no pattern can be determined.
    """
    if not reference_contacts:
        return None

    # Collect (name, email, zi_engage) tuples with valid data
    valid_refs = []
    for ref in reference_contacts:
        name_r  = (ref.get("name")  or "").strip()
        email_r = (ref.get("email") or "").strip()
        engage  = bool(ref.get("zi_engage", False))
        if not name_r or not email_r or "@" not in email_r:
            continue
        f, l = _name_parts(name_r)
        if not f:
            continue
        valid_refs.append({"name": name_r, "first": f, "last": l,
                           "email": email_r, "zi_engage": engage})

    if not valid_refs:
        return None

    # Extract consistent domain across references
    domains = [_extract_domain(r["email"]) for r in valid_refs]
    domain  = max(set(domains), key=domains.count)   # most common domain

    # Try each pattern against each reference
    pattern_votes: dict[str, list[dict]] = {}
    for ref in valid_refs:
        if _extract_domain(ref["email"]) != domain:
            continue  # skip cross-domain refs (e.g., acquired subsidiaries)
        local = _local_part(ref["email"])
        f, l  = ref["first"], ref["last"]
        for key, fmt in EMAIL_PATTERNS:
            candidate_local = fmt(f, l)
            if candidate_local and candidate_local == local:
                if key not in pattern_votes:
                    pattern_votes[key] = []
                pattern_votes[key].append(ref)
                break  # first pattern that matches wins for this ref

    if not pattern_votes:
        return None

    # Pick the pattern with the most votes; prefer engage-verified refs on tie
    def vote_score(item):
        key, refs = item
        engage_count = sum(1 for r in refs if r["zi_engage"])
        return (len(refs), engage_count)

    best_pattern, best_refs = max(pattern_votes.items(), key=vote_score)
    match_count   = len(best_refs)
    engage_any    = any(r["zi_engage"] for r in best_refs)

    if match_count >= 3 or (match_count >= 2 and engage_any):
        confidence = "high"
    elif match_count == 2 or (match_count == 1 and engage_any):
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "pattern":         best_pattern,
        "domain":          domain,
        "confidence":      confidence,
        "match_count":     match_count,
        "engage_verified": engage_any,
        "references": [
            {
                "name":            r["name"],
                "email":           r["email"],
                "zi_engage":       r["zi_engage"],
                "matched_pattern": best_pattern,
            }
            for r in best_refs
        ],
    }


def apply_pattern(name: str, pattern_key: str, domain: str) -> str:
    """
    Construct a candidate email for `name` using the detected pattern.
    Returns empty string if construction fails.
    """
    f, l = _name_parts(name)
    if not f:
        return ""
    fmt_fn = dict(EMAIL_PATTERNS).get(pattern_key)
    if not fmt_fn:
        return ""
    local = fmt_fn(f, l)
    if not local:
        return ""
    return f"{local}@{domain}"


def build_email_inference(
    contact: dict,
    company: str,
    account_contacts: list[dict],
) -> dict | None:
    """
    For a PHONE_ONLY contact, attempt to infer a candidate email by:
      1. Collecting reference contacts at the same company that have valid emails
      2. Prioritising ZI-engage-validated references
      3. Detecting the pattern; constructing a candidate
      4. Returning a structured inference block Claude can act on

    Returns None if no inference is possible (no reference emails at all).
    """
    name = contact.get("name", "").strip()
    if not name:
        return None

    # Build reference list from other contacts at this account that have emails
    references = []
    for other in account_contacts:
        if other is contact:
            continue
        other_email = (other.get("email") or "").strip()
        other_name  = (other.get("name")  or "").strip()
        if not other_email or "@" not in other_email or not other_name:
            continue
        # zi_engage: True if ZoomInfo flagged this email as valid/engaged
        references.append({
            "name":      other_name,
            "email":     other_email,
            "zi_engage": bool(other.get("zi_engage", False)),
        })

    if not references:
        return None

    result = detect_email_pattern(references)
    if not result:
        return None

    candidate = apply_pattern(name, result["pattern"], result["domain"])
    if not candidate:
        return None

    return {
        "candidate":        candidate,
        "pattern":          result["pattern"],
        "domain":           result["domain"],
        "confidence":       result["confidence"],
        "match_count":      result["match_count"],
        "engage_verified":  result["engage_verified"],
        "references":       result["references"],
        "validation_step": (
            f"Run enrich_contacts with name='{name}', company='{company}', "
            f"email='{candidate}' to verify before sending. "
            f"Do NOT send to this address until ZoomInfo confirms it."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment strategy builder
# ─────────────────────────────────────────────────────────────────────────────

def build_enrichment_task(
    contact: dict,
    company: str,
    tier: str,
    account_contacts: list[dict] | None = None,
) -> dict | None:
    """Build an enrichment task dict. Returns None for FULL contacts (no task needed)."""
    if tier == TIER_FULL:
        return None

    name     = contact.get("name", "")
    title    = contact.get("title", "")
    email    = (contact.get("email") or "").strip()
    phone_v  = best_phone(contact)
    linkedin = (contact.get("linkedin") or "").strip()

    if tier == TIER_EMAIL_ONLY:
        # Have email, need phone
        gap      = "phone"
        zi_hint  = f"enrich_contacts with name='{name}', company='{company}', email='{email}'"
        wsq      = f'"{name}" "{company}" direct dial OR phone number'
        fallback = "email_sequence_only"
        strategy = "zoominfo_enrich"
        inference = None

    elif tier == TIER_PHONE_ONLY:
        # Have phone, need email — attempt pattern inference first
        gap      = "email"
        wsq      = f'"{name}" "{company}" email'
        fallback = "linkedin_only"

        inference = build_email_inference(contact, company, account_contacts or [])

        if inference:
            # We have a candidate — upgrade strategy and craft a validation-aware ZI hint
            strategy = "email_pattern_inference"
            candidate = inference["candidate"]
            zi_hint = (
                f"enrich_contacts with name='{name}', company='{company}', "
                f"email='{candidate}' (inferred — validate before sending)"
            )
        else:
            # No reference emails at this company — fall back to standard ZI enrich
            strategy = "zoominfo_enrich"
            zi_hint  = f"enrich_contacts with name='{name}', company='{company}', phone='{phone_v}'"

    else:  # SHELL
        # Have nothing useful — try full re-enrich, then replacement
        gap      = "email + phone"
        zi_hint  = f"enrich_contacts with name='{name}', company='{company}', title='{title}'"
        wsq      = f'"{name}" "{company}" contact information'
        fallback = "replace_contact"
        strategy = "zoominfo_enrich" if (name and company) else "replace_contact"
        inference = None

    task = {
        "company":            company,
        "name":               name,
        "title":              title,
        "gap":                gap,
        "tier":               tier,
        "has_email":          bool(email),
        "has_phone":          bool(phone_v),
        "email":              email or None,
        "phone":              phone_v or None,
        "linkedin":           linkedin or None,
        "strategy":           strategy,
        "zoominfo_hint":      zi_hint,
        "web_search_query":   wsq,
        "fallback_strategy":  fallback,
    }
    if inference:
        task["email_inference"] = inference
    return task


# ─────────────────────────────────────────────────────────────────────────────
# Recommended primary contact selection
# ─────────────────────────────────────────────────────────────────────────────

# Title keywords ranked by seniority / decision-making authority
TITLE_PRIORITY = [
    # C-suite
    ["ceo", "chief executive"],
    ["coo", "chief operating"],
    ["cto", "chief technology"],
    ["cfo", "chief financial"],
    ["ciso", "chief information security"],
    ["cio", "chief information"],
    # VP
    ["vp ", "vice president", "vp,", "v.p."],
    # Director
    ["director"],
    # Manager
    ["manager", "head of"],
    # Other
    ["lead", "senior", "principal"],
]


def title_rank(title: str) -> int:
    """Lower = higher priority."""
    t = title.lower()
    for rank, keywords in enumerate(TITLE_PRIORITY):
        if any(kw in t for kw in keywords):
            return rank
    return len(TITLE_PRIORITY)


def pick_primary(contacts_by_tier: dict) -> dict | None:
    """
    Pick the best contact to lead outreach:
    1. Prefer FULL data
    2. Then EMAIL_ONLY (usable for sequences)
    3. Within tier: rank by title seniority
    """
    for tier in (TIER_FULL, TIER_EMAIL_ONLY, TIER_PHONE_ONLY):
        pool = contacts_by_tier.get(tier, [])
        if pool:
            return sorted(pool, key=lambda c: title_rank(c.get("title", "")))[0]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Main processor
# ─────────────────────────────────────────────────────────────────────────────

def process_contacts(data: dict) -> dict:
    all_accounts        = []
    enrichment_queue    = []
    total               = 0
    geo_excluded_total  = 0
    tier_counts         = {TIER_FULL: 0, TIER_EMAIL_ONLY: 0, TIER_PHONE_ONLY: 0, TIER_SHELL: 0}

    for account in data.get("accounts", []):
        company  = account.get("company", "Unknown")
        contacts = account.get("contacts", [])

        by_tier = {
            TIER_FULL:       [],
            TIER_EMAIL_ONLY: [],
            TIER_PHONE_ONLY: [],
            TIER_SHELL:      [],
            TIER_GEO:        [],
        }

        for contact in contacts:
            total += 1
            accepted, region = is_us_canada(contact)
            contact["_region"] = region

            if not accepted:
                geo_excluded_total += 1
                by_tier[TIER_GEO].append(contact)
                continue

            tier = classify(contact)
            contact["_tier"] = tier
            by_tier[tier].append(contact)
            tier_counts[tier] += 1

            # Pass full account contacts list so PHONE_ONLY can use peer emails
            task = build_enrichment_task(contact, company, tier, account_contacts=contacts)
            if task:
                enrichment_queue.append(task)

        primary = pick_primary(by_tier)

        all_accounts.append({
            "company":                company,
            "contacts_full":          by_tier[TIER_FULL],
            "contacts_email_only":    by_tier[TIER_EMAIL_ONLY],
            "contacts_phone_only":    by_tier[TIER_PHONE_ONLY],
            "contacts_shell":         by_tier[TIER_SHELL],
            "contacts_geo_excluded":  by_tier[TIER_GEO],
            "recommended_primary":    primary,
        })

    # Sort enrichment queue: PHONE_ONLY first (harder, email-less contacts at risk
    # of falling off sequences), then EMAIL_ONLY, then SHELL
    tier_order = {TIER_PHONE_ONLY: 1, TIER_EMAIL_ONLY: 2, TIER_SHELL: 3}
    enrichment_queue.sort(key=lambda t: (tier_order.get(t["tier"], 9), title_rank(t.get("title", ""))))

    # Add priority index
    for i, task in enumerate(enrichment_queue, start=1):
        task["priority"] = i

    us_ca_total       = total - geo_excluded_total
    enrichment_needed = tier_counts[TIER_EMAIL_ONLY] + tier_counts[TIER_PHONE_ONLY] + tier_counts[TIER_SHELL]
    ready             = tier_counts[TIER_FULL]

    return {
        "summary": {
            "total_contacts":      total,
            "geo_excluded":        geo_excluded_total,
            "us_canada_total":     us_ca_total,
            "full":                tier_counts[TIER_FULL],
            "email_only":          tier_counts[TIER_EMAIL_ONLY],
            "phone_only":          tier_counts[TIER_PHONE_ONLY],
            "shell":               tier_counts[TIER_SHELL],
            "enrichment_needed":   enrichment_needed,
            "ready_for_outreach":  ready,
        },
        "accounts":         all_accounts,
        "enrichment_queue": enrichment_queue,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard formatter
# ─────────────────────────────────────────────────────────────────────────────

def tier_bar(count: int, total: int, width: int = 20) -> str:
    if total == 0:
        return "░" * width
    filled = round((count / total) * width)
    return "█" * filled + "░" * (width - filled)


STRATEGY_LABELS = {
    "email_pattern_inference": "Email pattern inference (construct + ZI-validate)",
    "zoominfo_enrich":         "ZoomInfo re-enrich",
    "web_search":              "Web search fallback",
    "replace_contact":         "Find replacement contact",
    "email_sequence_only":     "Email-only outreach (no phone needed)",
}

FALLBACK_LABELS = {
    "email_sequence_only": "usable for email sequences as-is",
    "linkedin_only":       "LinkedIn outreach if email stays missing",
    "replace_contact":     "find a different contact at the company",
}


def format_dashboard(output: dict) -> str:
    W   = 58
    sep = "═" * W
    div = "─" * W
    s   = output["summary"]
    uca = s["us_canada_total"]

    lines = [
        "",
        sep,
        "  Contact Enrichment Report",
        sep,
        f"  Total contacts from ZoomInfo   : {s['total_contacts']}",
        f"  Geography excluded (non-US/CA) : {s['geo_excluded']}",
        f"  US / Canada contacts           : {uca}",
        div,
        f"  {'FULL (email + phone)':<30} {s['full']:>3}  {tier_bar(s['full'], uca)}",
        f"  {'EMAIL ONLY (no phone)':<30} {s['email_only']:>3}  {tier_bar(s['email_only'], uca)}",
        f"  {'PHONE ONLY (no email)':<30} {s['phone_only']:>3}  {tier_bar(s['phone_only'], uca)}",
        f"  {'SHELL (no email or phone)':<30} {s['shell']:>3}  {tier_bar(s['shell'], uca)}",
        div,
        f"  Ready for outreach             : {s['ready_for_outreach']}",
        f"  Enrichment tasks queued        : {s['enrichment_needed']}",
        sep,
    ]

    # Per-account summary
    lines += ["", "  By account:", ""]
    for acc in output["accounts"]:
        company = acc["company"]
        full    = len(acc["contacts_full"])
        eo      = len(acc["contacts_email_only"])
        po      = len(acc["contacts_phone_only"])
        sh      = len(acc["contacts_shell"])
        geo     = len(acc["contacts_geo_excluded"])
        prim    = acc.get("recommended_primary")

        tier_str = []
        if full: tier_str.append(f"{full} full")
        if eo:   tier_str.append(f"{eo} email-only")
        if po:   tier_str.append(f"{po} phone-only")
        if sh:   tier_str.append(f"{sh} shell")
        if geo:  tier_str.append(f"{geo} geo-excluded")
        lines.append(f"  {company}")
        lines.append(f"    {', '.join(tier_str) if tier_str else 'no contacts'}")
        if prim:
            prim_name  = prim.get("name", "Unknown")
            prim_title = prim.get("title", "")
            prim_tier  = prim.get("_tier", "")
            tier_note  = {"FULL": "full data", "EMAIL_ONLY": "email only", "PHONE_ONLY": "phone only"}.get(prim_tier, "")
            lines.append(f"    → Lead: {prim_name} ({prim_title}) [{tier_note}]")
        lines.append("")

    # Enrichment queue
    eq = output["enrichment_queue"]
    if eq:
        lines += [div, f"  Enrichment Queue ({len(eq)} tasks — in priority order):", ""]
        for task in eq:
            gap_icon = {"email": "✉", "phone": "☎", "email + phone": "✉☎"}.get(task["gap"], "?")
            lines.append(f"  #{task['priority']}  {task['company']}  •  {task['name']}  ({task['title']})")
            lines.append(f"     Gap: {gap_icon} {task['gap'].upper()}")
            lines.append(f"     Strategy : {STRATEGY_LABELS.get(task['strategy'], task['strategy'])}")

            inf = task.get("email_inference")
            if inf:
                conf_icon = {"high": "●●●", "medium": "●●○", "low": "●○○"}.get(inf["confidence"], "?")
                lines.append(f"     ✉ Inferred candidate : {inf['candidate']}")
                lines.append(f"       Pattern    : {inf['pattern']}   Confidence: {conf_icon} {inf['confidence'].upper()}")
                lines.append(f"       Based on   : {inf['match_count']} reference email(s)" +
                             (" [ZI-engage verified]" if inf["engage_verified"] else ""))
                for ref in inf["references"]:
                    engage_tag = " ✓engage" if ref["zi_engage"] else ""
                    lines.append(f"         → {ref['name']} <{ref['email']}>{engage_tag}")
                lines.append(f"       ⚠ Validate : {inf['validation_step']}")
            else:
                lines.append(f"     ZI hint  : {task['zoominfo_hint']}")

            lines.append(f"     Web query: {task['web_search_query']}")
            fallback = task.get("fallback_strategy", "")
            if fallback and fallback != task["strategy"]:
                lines.append(f"     Fallback : {FALLBACK_LABELS.get(fallback, fallback)}")
            lines.append("")
    else:
        lines += [div, "  No enrichment needed — all contacts have full data.", ""]

    lines += [sep, ""]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    input_path = sys.argv[1]
    quiet      = "--quiet" in sys.argv

    if not os.path.exists(input_path):
        print(f"ERROR: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    output = process_contacts(data)

    if not quiet:
        print(format_dashboard(output), file=sys.stderr)

    print(json.dumps(output, indent=2, ensure_ascii=False))

    # Exit code: 0 = all full, 1 = some enrichment needed, 2 = all contacts excluded/shell
    s = output["summary"]
    if s["us_canada_total"] == 0:
        sys.exit(2)
    elif s["enrichment_needed"] > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
