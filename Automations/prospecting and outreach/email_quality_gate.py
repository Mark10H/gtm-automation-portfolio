"""
email_quality_gate.py — Moxo BDR Email Quality Gate
=====================================================
Scans all generated email sequences for quality issues before the rep
approves final send. Catches spam triggers, formatting bugs, banned
filler phrases, placeholder tokens, structural problems, and
step-specific CTA violations.

Severity levels:
  BLOCK — must be fixed before emails are shown to rep / sent
  WARN  — should be fixed, rep can override
  INFO  — cosmetic / low-signal suggestions

Overall status per email:
  PASS  — zero issues
  WARN  — only WARN/INFO issues
  BLOCK — at least one BLOCK issue

Overall batch status:
  PASS  — all emails passed
  WARN  — some emails have warnings, none are blocked
  BLOCK — at least one email is blocked

─────────────────────────────────────────────────────────
INPUT JSON (file path passed as first CLI arg):
{
  "emails": [
    {
      "company":       "Acme Financial",
      "short_name":    "Acme",           // optional — overrides auto-token logic for company matching
      "contact_name":  "Jane Smith",
      "contact_title": "COO",
      "sequence": [
        { "step": 1, "subject": "...", "body": "..." },
        { "step": 2, "subject": "...", "body": "..." },
        { "step": 3, "subject": "...", "body": "..." },
        { "step": 4, "subject": "...", "body": "..." }
      ]
    }
  ]
}

OUTPUT JSON (stdout):
{
  "summary": {
    "total_emails": 20,
    "total_sequences": 5,
    "passed": 16,
    "warned": 3,
    "blocked": 1,
    "overall_status": "BLOCK"
  },
  "results": [
    {
      "company":      "Acme Financial",
      "contact_name": "Jane Smith",
      "step":         1,
      "status":       "BLOCK",
      "subject":      "...",
      "word_count":   87,
      "issues": [
        {
          "severity": "BLOCK",
          "rule":     "banned_filler",
          "detail":   "Contains banned opener: 'I hope this email finds you well'"
        }
      ]
    }
  ],
  "blocked_list":  ["Acme Financial — Step 1", "Summit Insurance — Step 3"],
  "warned_list":   ["BrightPath Realty — Step 2"]
}

USAGE:
  python scripts/email_quality_gate.py <input_json_path>
  python scripts/email_quality_gate.py <input_json_path> --quiet   (suppress dashboard)
"""

import sys
import json
import re
import os

# ─────────────────────────────────────────────────────────────────────────────
# KB Proof Point Validation
# ─────────────────────────────────────────────────────────────────────────────

# Path to the Moxo KB — resolved relative to this script's location
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_KB_PATH = os.path.join(_SCRIPT_DIR, "..", "references", "moxo-kb.md")

# Patterns that match numeric claims in email bodies:
#   "54%", "10x", "$200M", "81% reduction", "2 hours", "4 days", etc.
_STAT_PATTERNS = [
    r"\d+(?:\.\d+)?%",                        # percentages: 54%, 81%, 29%
    r"\d+(?:\.\d+)?x\b",                      # multipliers: 10x, 4x
    r"\$\d[\d,]*(?:\.\d+)?[kKmMbB]?\+?",      # dollar amounts: $200M, $175k+, $90k
    r"\d+(?:\.\d+)?[-–]\d+(?:\.\d+)?%",        # ranges: 72–75%
    r"\d+[-–]\d+\s*(?:weeks?|days?|hours?|months?)", # time ranges: 6–10 weeks, 3–5 weeks
    r"\d+\s*(?:weeks?|days?|hours?)\b",        # time: 2 hours, 4 days
]

_STAT_RE = re.compile("|".join(f"({p})" for p in _STAT_PATTERNS), re.IGNORECASE)

# Stats that are inherently generic / not Moxo proof points — skip these
_GENERIC_STATS = {
    "100%",   # used in general context ("100% on service deliverables" is KB, but "100%" alone is generic)
    "0%",
    "1x",
}

# Cache for the parsed KB whitelist
_kb_stats_cache: set | None = None
_kb_quotes_cache: list | None = None


def _load_kb() -> tuple[set, list]:
    """Parse moxo-kb.md and extract all approved stats and customer quotes."""
    global _kb_stats_cache, _kb_quotes_cache
    if _kb_stats_cache is not None:
        return _kb_stats_cache, _kb_quotes_cache

    approved_stats: set = set()
    approved_quotes: list = []

    if not os.path.exists(_KB_PATH):
        # KB not found — cannot validate; return empty (checks will be skipped)
        _kb_stats_cache = set()
        _kb_quotes_cache = []
        return _kb_stats_cache, _kb_quotes_cache

    with open(_KB_PATH, "r", encoding="utf-8") as f:
        kb_text = f.read()

    # Extract every stat-like token from the KB
    for m in _STAT_RE.finditer(kb_text):
        stat = m.group(0).strip().lower()
        # Normalise dashes to en-dash for consistent matching
        stat = stat.replace("–", "-").replace("—", "-")
        approved_stats.add(stat)

    # Also add some common restatements found in the KB
    # (e.g., "cut in half" = 50%, "tenfold" = 10x)
    WORD_STATS = {
        "cut in half": True, "tenfold": True, "ten-fold": True,
        "75%": True,  # "almost cut onboarding by 75%"
    }
    for ws in WORD_STATS:
        approved_stats.add(ws)

    # Extract quoted strings (customer quotes from Section 5 and Section 6)
    quote_re = re.compile(r'"([^"]{20,})"')
    for m in quote_re.finditer(kb_text):
        approved_quotes.append(m.group(1).strip().lower())

    _kb_stats_cache = approved_stats
    _kb_quotes_cache = approved_quotes
    return _kb_stats_cache, _kb_quotes_cache


def _normalise_stat(stat: str) -> str:
    """Normalise a stat string for comparison."""
    s = stat.strip().lower()
    s = s.replace("–", "-").replace("—", "-")
    return s


def check_proof_points(body: str) -> list[dict]:
    """
    Scan email body for numeric claims (percentages, multipliers, dollar amounts,
    time comparisons) and verify each one exists in moxo-kb.md.

    Severity: BLOCK — any stat not found in the KB is a potential hallucination.
    """
    approved_stats, _ = _load_kb()

    # If KB couldn't be loaded, skip validation but warn
    if not approved_stats:
        return [{
            "severity": "WARN",
            "rule":     "kb_not_loaded",
            "detail":   "Could not load moxo-kb.md — proof point validation skipped. Verify stats manually.",
        }]

    issues = []
    seen = set()

    for m in _STAT_RE.finditer(body):
        raw = m.group(0).strip()
        norm = _normalise_stat(raw)

        # Skip generic numbers and duplicates
        if norm in _GENERIC_STATS or norm in seen:
            continue
        seen.add(norm)

        # Check if this stat exists in the KB whitelist
        if norm not in approved_stats:
            # Try a looser match: strip trailing punctuation, check if the
            # number portion appears in any approved stat
            found = False
            for approved in approved_stats:
                if norm in approved or approved in norm:
                    found = True
                    break
            if not found:
                issues.append({
                    "severity": "BLOCK",
                    "rule":     "unverified_proof_point",
                    "detail":   (
                        f"Stat '{raw}' not found in moxo-kb.md. "
                        f"This may be a hallucinated proof point. "
                        f"Only use stats that appear verbatim in the KB (Sections 4, 5, or 6)."
                    ),
                })

    return issues


def check_customer_quotes(body: str) -> list[dict]:
    """
    Scan for quoted strings in the email body that look like customer quotes
    and verify they exist in moxo-kb.md Section 5 or 6.

    Severity: BLOCK — fabricated customer quotes destroy credibility.
    """
    _, approved_quotes = _load_kb()

    if not approved_quotes:
        return []  # KB not loaded — already warned in check_proof_points

    issues = []
    # Find quoted strings in the email body (20+ chars to avoid matching short phrases)
    body_quotes = re.findall(r'"([^"]{20,})"', body)

    for bq in body_quotes:
        bq_norm = bq.strip().lower()
        # Check if this quote (or a substantial substring) exists in the KB
        matched = False
        for aq in approved_quotes:
            # Allow partial match: if 60%+ of the quote words overlap
            bq_words = set(bq_norm.split())
            aq_words = set(aq.split())
            if len(bq_words) < 5:
                continue  # too short to be a customer quote attribution
            overlap = len(bq_words & aq_words) / len(bq_words)
            if overlap >= 0.6:
                matched = True
                break
        if not matched and len(bq.split()) >= 8:
            # Only flag quotes that are 8+ words — shorter ones are likely not customer quotes
            issues.append({
                "severity": "BLOCK",
                "rule":     "unverified_customer_quote",
                "detail":   (
                    f"Quoted text not found in moxo-kb.md: \"{bq[:60]}{'...' if len(bq) > 60 else ''}\". "
                    f"Only use customer quotes from KB Section 5 or Section 6 industry playbooks."
                ),
            })

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Rule tables
# ─────────────────────────────────────────────────────────────────────────────

# BLOCK: filler openers that immediately signal generic templating
BANNED_OPENERS = [
    "i hope this email finds you well",
    "i hope this finds you well",
    "i hope you're doing well",
    "i hope you are doing well",
    "i wanted to reach out",
    "i am reaching out",
    "i'm reaching out",
    "just wanted to touch base",
    "just touching base",
    "just following up",
    "i'm following up",
    "i am following up",
    "i wanted to follow up",
    "as per my last email",
    "per my previous email",
    "circling back",
    "looping back",
    "bumping this up",
    "to revisit",
]

# BLOCK: spam-trigger words (deliverability killers in cold email)
SPAM_TRIGGERS = [
    "guaranteed",
    "risk-free",
    "risk free",
    "act now",
    "limited time",
    "no obligation",
    "100% free",
    "free trial",           # ok in later nurture, not cold step 1-4
    "click here",
    "unsubscribe",          # never write it manually — ESPs add it
    "you have been selected",
    "congratulations",
    "dear friend",
    "make money",
    "earn money",
    "extra income",
    "this is not spam",
    "not spam",
    "cash",                 # narrow — only flag if standalone word
    "winner",
    "prize",
    "urgent",
    "important notice",
]

# BLOCK: placeholder tokens that weren't filled in
PLACEHOLDER_PATTERNS = [
    r"\[.*?\]",             # [COMPANY], [NAME], [TITLE], [INSERT HERE]
    r"\{\{.*?\}\}",         # {{company}}, {{first_name}}
    r"\{[A-Z_]{2,}\}",      # {COMPANY_NAME}
    r"<[A-Z_]{2,}>",        # <PAIN_POINT>
    r"INSERT\s+\w+\s+HERE", # INSERT COMPANY NAME HERE
    r"YOUR\s+(COMPANY|NAME|TITLE|INDUSTRY)\b",
    r"PLACEHOLDER",
]

# WARN: corporate buzzwords that dilute credibility
BUZZWORDS = [
    "synergy",
    "synergies",
    "leverage",             # only flag as verb: "leverage our platform" — checked contextually
    "paradigm",
    "disruptive",
    "disrupting",
    "game.?changer",
    "game.?changing",
    "thought leader",
    "thought leadership",
    "best.?in.?class",
    "world.?class",
    "cutting.?edge",
    "bleeding.?edge",
    "innovative solution",
    "holistic approach",
    "robust solution",
    "seamless solution",
    "move the needle",
    "boil the ocean",
    "low.?hanging fruit",
    "deep.?dive",
    "take it offline",
    "bandwidth",            # only if used metaphorically — flagged contextually
    "ping me",
    "circle back",
    "double.?click",        # "let's double-click into that"
    "ideate",
    "learnings",
    "actionable insights",
    "value.?add(?:ed)?",
]

# BLOCK: dashes and arrows are absolutely banned in all email content.
# They look robotic and templated. Rewrite using periods, commas, or separate sentences.
BANNED_DASH_ARROW_CHARS = {
    "\u2014": "em dash found. Dashes are banned in emails. Rewrite using a period, comma, or separate sentence.",
    "\u2013": "en dash found. Dashes are banned in emails. Rewrite using a period, comma, or separate sentence.",
    "\u2192": "arrow (\\u2192) found. Arrows are banned in emails. Rewrite using natural language.",
    "\u279c": "arrow (\\u279c) found. Arrows are banned in emails. Rewrite using natural language.",
    "\u27a1": "arrow (\\u27a1) found. Arrows are banned in emails. Rewrite using natural language.",
}

# Regex patterns for ASCII dashes-as-punctuation and ASCII arrows in email content
BANNED_DASH_ARROW_PATTERNS = [
    (r"\s--\s", "double hyphen (--) used as dash. Dashes are banned in emails. Rewrite using a period, comma, or separate sentence."),
    (r"\s-\s", "hyphen used as dash (word - word). Dashes are banned in emails. Rewrite using a period, comma, or separate sentence."),
    (r"->", "arrow (->) found. Arrows are banned in emails. Rewrite using natural language like 'leads to' or 'results in'."),
    (r"-->", "arrow (-->) found. Arrows are banned in emails. Rewrite using natural language."),
    (r"=>", "arrow (=>) found. Arrows are banned in emails. Rewrite using natural language."),
]

# WARN: formatting characters that render badly in email clients
RENDER_RISK_CHARS = {
    "\u2018": "left single curly quote (') — may render as ?. Use straight apostrophe.",
    "\u2019": "right single curly quote (') — may render as ?. Use straight apostrophe.",
    "\u201c": 'left double curly quote (") — may render as ?. Use straight quote.',
    "\u201d": 'right double curly quote (") — may render as ?. Use straight quote.',
    "\u2026": "ellipsis character (…) — use three plain dots instead.",
    "\u00a0": "non-breaking space — can cause weird line breaks.",
    "\u200b": "zero-width space — invisible but causes deliverability issues.",
}

# Subject line max length (chars)
SUBJECT_MAX_CHARS = 50

# Body word count bounds
BODY_MIN_WORDS         = 30   # Steps 1–3
BODY_MIN_WORDS_BREAKUP = 15   # Step 4 — concise breakup emails are intentional style
BODY_MAX_WORDS = 150

# Step 1 hard CTA — should not ask for a meeting on first touch
HARD_CTA_PATTERNS = [
    r"\b(?:schedule|book|set up|set-up|calendar|calendly|30.?min|15.?min|quick call|demo|jump on)\b",
]

# BLOCK: fewer than 2 sentences in the body (not substantive)
BODY_MIN_SENTENCES = 2

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def word_count(text: str) -> int:
    return len(text.split())


def sentence_count(text: str) -> int:
    # Rough: split on . ! ? followed by whitespace or end
    return max(1, len(re.findall(r"[.!?]+(?:\s|$)", text)))


def normalise(text: str) -> str:
    """Lowercase and collapse whitespace for phrase matching."""
    return re.sub(r"\s+", " ", text.lower().strip())


def contains_phrase(text_norm: str, phrase: str) -> bool:
    """Check if normalised text contains an exact phrase (word-boundary aware)."""
    # Escape for regex, then ensure we don't match mid-word
    escaped = re.escape(phrase)
    return bool(re.search(r"(?<!\w)" + escaped + r"(?!\w)", text_norm))


def check_dashes_arrows(text: str) -> list[dict]:
    """
    BLOCK check: dashes and arrows are absolutely banned in email content.
    They look robotic, templated, and signal AI-generated copy.
    """
    issues = []

    # Check Unicode dash/arrow characters
    for char, explanation in BANNED_DASH_ARROW_CHARS.items():
        if char in text:
            count = text.count(char)
            issues.append({
                "severity": "BLOCK",
                "rule":     "banned_dash_or_arrow",
                "detail":   f"{explanation} Found {count}x in this email.",
            })

    # Check ASCII dash-as-punctuation and arrow patterns
    for pattern, explanation in BANNED_DASH_ARROW_PATTERNS:
        if re.search(pattern, text):
            issues.append({
                "severity": "BLOCK",
                "rule":     "banned_dash_or_arrow",
                "detail":   explanation,
            })

    return issues


def find_render_risk(text: str) -> list[dict]:
    issues = []
    for char, explanation in RENDER_RISK_CHARS.items():
        if char in text:
            count = text.count(char)
            issues.append({
                "severity": "WARN",
                "rule":     "render_risk_char",
                "detail":   f"{explanation} Found {count}× in this email.",
            })
    return issues


def check_placeholders(subject: str, body: str) -> list[dict]:
    combined = subject + " " + body
    issues = []
    for pattern in PLACEHOLDER_PATTERNS:
        matches = re.findall(pattern, combined, re.IGNORECASE)
        if matches:
            unique = list(dict.fromkeys(matches))[:3]
            issues.append({
                "severity": "BLOCK",
                "rule":     "unfilled_placeholder",
                "detail":   f"Unfilled placeholder token(s): {', '.join(unique)}",
            })
    return issues


def check_banned_openers(body_norm: str) -> list[dict]:
    issues = []
    for phrase in BANNED_OPENERS:
        if contains_phrase(body_norm, phrase):
            issues.append({
                "severity": "BLOCK",
                "rule":     "banned_opener",
                "detail":   f"Contains banned filler opener: '{phrase}'",
            })
    return issues


def check_spam_triggers(subject_norm: str, body_norm: str) -> list[dict]:
    combined = subject_norm + " " + body_norm
    issues = []
    for phrase in SPAM_TRIGGERS:
        if contains_phrase(combined, phrase):
            issues.append({
                "severity": "BLOCK",
                "rule":     "spam_trigger",
                "detail":   f"Contains spam-trigger word/phrase: '{phrase}'",
            })
    return issues


def check_buzzwords(body_norm: str) -> list[dict]:
    issues = []
    seen = set()
    for pattern in BUZZWORDS:
        matches = re.findall(r"(?<!\w)" + pattern + r"(?!\w)", body_norm)
        if matches:
            word = matches[0]
            if word not in seen:
                seen.add(word)
                issues.append({
                    "severity": "WARN",
                    "rule":     "buzzword",
                    "detail":   f"Corporate buzzword detected: '{word}' — replace with specific language.",
                })
    return issues


def check_subject_length(subject: str) -> list[dict]:
    issues = []
    if len(subject) == 0:
        issues.append({
            "severity": "BLOCK",
            "rule":     "missing_subject",
            "detail":   "Subject line is empty.",
        })
    elif len(subject) > SUBJECT_MAX_CHARS:
        issues.append({
            "severity": "WARN",
            "rule":     "subject_too_long",
            "detail":   f"Subject is {len(subject)} chars (recommended ≤{SUBJECT_MAX_CHARS}). Long subjects get cut off on mobile.",
        })
    return issues


def check_body_length(body: str, step: int = 0) -> list[dict]:
    issues = []
    wc  = word_count(body)
    sc  = sentence_count(body)
    min_words = BODY_MIN_WORDS_BREAKUP if step == 4 else BODY_MIN_WORDS

    if wc < min_words:
        issues.append({
            "severity": "BLOCK",
            "rule":     "body_too_short",
            "detail":   f"Body is only {wc} words (min {min_words} for step {step}). Not substantive enough.",
        })
    elif wc > BODY_MAX_WORDS:
        issues.append({
            "severity": "WARN",
            "rule":     "body_too_long",
            "detail":   f"Body is {wc} words (recommended ≤{BODY_MAX_WORDS}). Cold emails should be concise.",
        })

    if sc < BODY_MIN_SENTENCES:
        issues.append({
            "severity": "BLOCK",
            "rule":     "body_too_few_sentences",
            "detail":   f"Body has only {sc} sentence(s). Needs at least {BODY_MIN_SENTENCES}.",
        })
    return issues


# Corporate suffixes stripped when building short-name tokens.
# A word that is ONLY a suffix does not count as a meaningful company reference.
CORP_SUFFIXES = {
    "group", "services", "inc", "llc", "corp", "corporation",
    "partners", "ventures", "management", "consulting", "solutions",
    "systems", "associates", "industries", "holdings", "advisors",
    "capital", "technologies", "technology", "enterprises",
    "international", "limited", "co", "company", "and", "the",
    # Vertical-specific suffixes common in the Moxo ICP
    "financial", "lending", "mortgage", "realty", "properties",
    "insurance", "investments", "investment", "banking", "wealth",
    "law", "legal", "practice", "software", "saas", "manufacturing",
}


def build_company_tokens(company: str, short_name: str = "") -> list[str]:
    """
    Build an ordered list of candidate tokens from the company's registered name.
    The gate passes the company check if ANY token is found in the email body.

    Token priority (checked in order):
      1. Explicit short_name override (e.g. "Acme" for "Acme Financial Partners")
      2. Full registered name           (e.g. "acme financial partners")
      3. De-suffixed name               (e.g. "acme" — all suffix words removed)
      4. First meaningful word ≥4 chars (e.g. "acme")

    Why this matters:
      "Starlight Mortgage Lending" → tokens: ["starlight mortgage lending", "starlight"]
      A rep writing "Starlight" in every email is correct — the check should pass.
      Only fire if NONE of these appear, meaning the email is truly generic.
    """
    tokens = []

    # 1. Explicit override always wins
    if short_name:
        tokens.append(short_name.lower().strip())

    norm = company.lower().strip()

    # 2. Full name
    if norm:
        tokens.append(norm)

    # 3. Strip punctuation, split, remove suffix words
    words = re.sub(r"[,.'\"()]", "", norm).split()
    meaningful = [w for w in words if w not in CORP_SUFFIXES and len(w) >= 3]

    if meaningful:
        desuffixed = " ".join(meaningful)
        if desuffixed != norm:
            tokens.append(desuffixed)

        # 4. First meaningful word ≥4 chars
        first = next((w for w in meaningful if len(w) >= 4), None)
        if first and first != desuffixed:
            tokens.append(first)

    # Deduplicate while preserving order
    seen: set = set()
    result = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def check_personalization(body_norm: str, company: str, contact_name: str,
                          short_name: str = "", step: int = 0) -> list[dict]:
    issues = []

    # Company check — passes if ANY candidate token appears in the body.
    # Severity depends on step:
    #   Step 1 → WARN: the cold open must reference the company; generic Step 1s are a red flag.
    #   Steps 2–4 → INFO: follow-ups often say "your team" / "the initiative" — still personalized.
    tokens = build_company_tokens(company, short_name)
    if tokens and not any(contains_phrase(body_norm, t) for t in tokens):
        checked   = ", ".join(f"'{t}'" for t in tokens[:3])
        severity  = "WARN" if step == 1 else "INFO"
        issues.append({
            "severity": severity,
            "rule":     "missing_company_name",
            "detail":   (
                f"Company name '{company}' does not appear in the body "
                f"(checked: {checked}). "
                + ("Step 1 should reference the prospect's company by name."
                   if step == 1
                   else "Consider naming the company to reinforce relevance.")
            ),
        })

    # Contact first name should appear at least once
    if contact_name:
        first = contact_name.split()[0].lower()
        if first and len(first) > 1 and first not in body_norm:
            issues.append({
                "severity": "INFO",
                "rule":     "missing_contact_name",
                "detail":   f"Contact first name '{contact_name.split()[0]}' not found in body. Consider personalizing the greeting.",
            })
    return issues


def check_step1_cta(body_norm: str, step: int) -> list[dict]:
    """Step 1 should not have a hard meeting/demo CTA on first touch."""
    if step != 1:
        return []
    issues = []
    for pattern in HARD_CTA_PATTERNS:
        if re.search(pattern, body_norm):
            match = re.search(pattern, body_norm).group(0)
            issues.append({
                "severity": "WARN",
                "rule":     "step1_hard_cta",
                "detail":   f"Step 1 contains a hard CTA ('{match}'). First touch should spark interest, not ask for a meeting.",
            })
            break  # one flag per email is enough
    return issues


def check_allcaps(body: str) -> list[dict]:
    """Flag ALL-CAPS words that are not known acronyms."""
    KNOWN_ACRONYMS = {"CRM", "API", "ROI", "KPI", "SLA", "AI", "ML", "IT",
                      "CEO", "CFO", "COO", "CTO", "VP", "HR", "ERP", "SaaS",
                      "B2B", "SMB", "GTM", "TOFU", "MOFU", "BOFU", "PDF",
                      "USA", "UK", "EU", "US", "FAQ", "FYI", "OOO", "PTO",
                      # Financial / insurance / real estate industry acronyms
                      "RIA", "AUM", "AUA", "BDR", "SDR", "AE", "CS", "CSM",
                      "MSA", "SOW", "NDA", "LOI", "MCA", "ACH", "KYC", "AML",
                      "FDIC", "SEC", "FINRA", "MGA", "TPA", "E&O", "P&C",
                      "CPA", "CFP", "CFA", "CLU", "ChFC", "CISA", "CISSP",
                      "SOC", "GDPR", "CCPA", "HIPAA", "PCI", "DSS", "MLS"}
    caps_words = re.findall(r"\b[A-Z]{3,}\b", body)
    flagged = [w for w in set(caps_words) if w not in KNOWN_ACRONYMS]
    if flagged:
        return [{
            "severity": "WARN",
            "rule":     "all_caps_word",
            "detail":   f"ALL-CAPS word(s) detected: {', '.join(sorted(flagged)[:5])}. Avoid shouting in cold email.",
        }]
    return []


def check_exclamation_abuse(subject: str, body: str) -> list[dict]:
    combined = subject + " " + body
    count = combined.count("!")
    if count > 2:
        return [{
            "severity": "WARN",
            "rule":     "exclamation_abuse",
            "detail":   f"{count} exclamation marks in this email. Keep to ≤2 total — heavy use reads as pushy.",
        }]
    return []


def check_missing_body(body: str) -> list[dict]:
    if not body or not body.strip():
        return [{
            "severity": "BLOCK",
            "rule":     "missing_body",
            "detail":   "Email body is empty.",
        }]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Per-email scanner
# ─────────────────────────────────────────────────────────────────────────────

def scan_email(company: str, contact_name: str, contact_title: str,
               step: int, subject: str, body: str,
               short_name: str = "") -> dict:
    issues = []

    # Run in severity order: BLOCK checks first, then WARN/INFO
    issues += check_missing_body(body)
    if not body.strip():
        # Can't run further checks without a body
        status = "BLOCK"
        return _build_result(company, contact_name, step, subject, body, status, issues)

    body_norm    = normalise(body)
    subject_norm = normalise(subject)

    # BLOCK checks
    issues += check_placeholders(subject, body)
    issues += check_banned_openers(body_norm)
    issues += check_spam_triggers(subject_norm, body_norm)
    issues += check_body_length(body, step)
    issues += check_subject_length(subject)
    issues += check_proof_points(body)
    issues += check_customer_quotes(body)
    issues += check_dashes_arrows(subject + " " + body)

    # WARN checks
    issues += find_render_risk(subject + " " + body)
    issues += check_buzzwords(body_norm)
    issues += check_personalization(body_norm, company, contact_name, short_name, step)
    issues += check_step1_cta(body_norm, step)
    issues += check_allcaps(body)
    issues += check_exclamation_abuse(subject, body)

    # Determine overall status — INFO does not elevate status
    if any(i["severity"] == "BLOCK" for i in issues):
        status = "BLOCK"
    elif any(i["severity"] == "WARN" for i in issues):
        status = "WARN"
    else:
        status = "PASS"

    return _build_result(company, contact_name, step, subject, body, status, issues)


def _build_result(company, contact_name, step, subject, body, status, issues):
    return {
        "company":      company,
        "contact_name": contact_name,
        "step":         step,
        "status":       status,
        "subject":      subject,
        "word_count":   word_count(body) if body else 0,
        "issues":       issues,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Batch runner
# ─────────────────────────────────────────────────────────────────────────────

def run_gate(data: dict) -> dict:
    all_results = []

    for entry in data.get("emails", []):
        company       = entry.get("company", "Unknown")
        short_name    = entry.get("short_name", "")
        contact_name  = entry.get("contact_name", "")
        contact_title = entry.get("contact_title", "")
        for step_obj in entry.get("sequence", []):
            step    = step_obj.get("step", 0)
            subject = step_obj.get("subject", "")
            body    = step_obj.get("body", "")
            result  = scan_email(company, contact_name, contact_title, step, subject, body, short_name)
            all_results.append(result)

    total    = len(all_results)
    passed   = sum(1 for r in all_results if r["status"] == "PASS")
    warned   = sum(1 for r in all_results if r["status"] == "WARN")
    blocked  = sum(1 for r in all_results if r["status"] == "BLOCK")

    sequences = len(data.get("emails", []))

    if blocked > 0:
        overall = "BLOCK"
    elif warned > 0:
        overall = "WARN"
    else:
        overall = "PASS"

    blocked_list = [f"{r['company']} — Step {r['step']}" for r in all_results if r["status"] == "BLOCK"]
    warned_list  = [f"{r['company']} — Step {r['step']}" for r in all_results if r["status"] == "WARN"]

    return {
        "summary": {
            "total_emails":     total,
            "total_sequences":  sequences,
            "passed":           passed,
            "warned":           warned,
            "blocked":          blocked,
            "overall_status":   overall,
        },
        "results":      all_results,
        "blocked_list": blocked_list,
        "warned_list":  warned_list,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard formatter
# ─────────────────────────────────────────────────────────────────────────────

STATUS_ICONS = {"PASS": "✓", "WARN": "⚠", "BLOCK": "✗"}
SEVERITY_ICONS = {"BLOCK": "✗", "WARN": "⚠", "INFO": "ℹ"}


def format_dashboard(output: dict) -> str:
    W   = 56
    sep = "═" * W
    div = "─" * W

    s = output["summary"]
    overall = s["overall_status"]

    overall_line = {
        "PASS":  "  ✓  ALL EMAILS PASSED — ready for rep review",
        "WARN":  "  ⚠  WARNINGS FOUND — rep review recommended",
        "BLOCK": "  ✗  BLOCKED EMAILS — must fix before proceeding",
    }[overall]

    lines = [
        "",
        sep,
        f"  Email Quality Gate",
        sep,
        f"  Total emails scanned  : {s['total_emails']}  ({s['total_sequences']} sequences)",
        f"  Passed                : {s['passed']}",
        f"  Warnings              : {s['warned']}",
        f"  Blocked               : {s['blocked']}",
        div,
        overall_line,
        sep,
    ]

    # Per-email breakdown (only non-PASS)
    non_pass = [r for r in output["results"] if r["status"] != "PASS"]
    if non_pass:
        lines += ["", "  Issues by email:", ""]
        for r in non_pass:
            icon = STATUS_ICONS[r["status"]]
            lines.append(f"  {icon}  {r['company']}  •  Step {r['step']}  •  {r['word_count']}w")
            for issue in r["issues"]:
                si = SEVERITY_ICONS[issue["severity"]]
                sev = issue["severity"]
                rule = issue["rule"]
                detail = issue["detail"]
                # Wrap detail at ~50 chars
                wrapped = _wrap(detail, 48, indent="        ")
                lines.append(f"     {si} [{sev}] {rule}")
                lines.append(f"        {wrapped}")
            lines.append("")

    if not non_pass:
        lines += ["", "  No issues found across all emails.", ""]

    lines.append(sep)
    lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int, indent: str = "") -> str:
    words = text.split()
    lines = []
    current = []
    length = 0
    for w in words:
        if length + len(w) + (1 if current else 0) > width:
            lines.append(" ".join(current))
            current = [w]
            length = len(w)
        else:
            current.append(w)
            length += len(w) + (1 if len(current) > 1 else 0)
    if current:
        lines.append(" ".join(current))
    return f"\n{indent}".join(lines)


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
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    output = run_gate(data)

    if not quiet:
        print(format_dashboard(output), file=sys.stderr)

    print(json.dumps(output, indent=2, ensure_ascii=False))

    # Exit code mirrors overall status
    status = output["summary"]["overall_status"]
    sys.exit(0 if status == "PASS" else (2 if status == "BLOCK" else 1))


if __name__ == "__main__":
    main()
