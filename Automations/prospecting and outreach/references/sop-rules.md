# Moxo BDR SOP Rules — Edge Cases & Examples
## Filtering Reference for `filter_accounts.py` and Manual Application

This document is the authoritative reference for account filtering decisions. Read it whenever a filtering edge case arises. The rules in Step 2 of the skill are the summary; this document covers the nuance, the grey areas, and the worked examples.

---

## Rule 1 — Headcount

**What it means:** Only work accounts whose employee count falls within the rep-confirmed range.

**Default range:** Ask the rep at the start of each session. There is no hard default — always confirm.

**Edge cases:**

| Scenario | Decision |
|----------|----------|
| HubSpot shows `Number of Employees` = blank | Do **not** automatically exclude. Check the company website or ZoomInfo during Step 3 research. If headcount still cannot be determined, include the account and note "Headcount unverified — included for research" in the Audit Log. |
| HubSpot shows a range (e.g., "201-500") | Use the midpoint (350) for comparison against the rep's preferred range. If the midpoint is in range, include. |
| Rep says "no preference" | Apply no headcount filter — include all sizes that pass the other rules. |
| Company lists 0 employees | Treat as unverified — same as blank. Do not exclude solely on this basis. |

**Example:**
> Rep confirms: "50 to 500 employees." HubSpot shows Acme Corp at 480 employees → include. BetaCo shows 520 → exclude, log: "Excluded — Outside Headcount Range (520 employees, rep range 50–500)."

---

## Rule 2 — Geography

**What it means:** Only US and Canada. All other countries excluded.

**Edge cases:**

| Scenario | Decision |
|----------|----------|
| Country/Region field is blank | Check the company domain (.ca = Canada, .com = ambiguous). If ambiguous, check the website during Step 3. If still unknown, include tentatively and note in Audit Log: "Geography unverified — assumed US based on .com domain." |
| Company HQ is US but has offices in other countries | Include — HQ geography is the determining factor. |
| Company HQ is in Canada but domain is .com | Include — Canada is in-scope. |
| Puerto Rico, US Virgin Islands | Include — US territories count. |
| Company lists "North America" without specifics | Include tentatively; verify during research. |

**Example:**
> GlobalTech Ltd shows "United Kingdom" → exclude, log: "Excluded — Outside Geography (UK)." CanadaCo shows "Canada" → include.

---

## Rule 3 — Active Customer

**What it means:** If the HubSpot property **"Is an Active customer?"** is any of **Yes**, skip entirely. These accounts are already in the customer success motion.

**Edge cases:**

| Scenario | Decision |
|----------|----------|
| Field is blank or null | Treat as **not** an active customer — include in filtering. |
| Field shows "Former Customer" | Include — former customers are valid outreach targets. |
| Rep says "I think they're a customer but it doesn't show Yes" | Trust the HubSpot field. If the rep has strong reason to believe otherwise, they can verify separately — do not exclude based on rep assumption alone. |

**Example:**
> PremiumWealth LLC shows HubSpot property "Is an Active customer?" is any of Yes → exclude, log: "Excluded — Active Customer." NextLevel Finance shows blank → include.

---

## Rule 4 — Already Enriched (AI Claude Enriched)

**What it means:** If `AI (Claude) Enriched?` = Yes, this account has already been researched in a prior session. Skip it to avoid duplicate outreach being built.

**Edge cases:**

| Scenario | Decision |
|----------|----------|
| Field is blank or no | Include — not yet researched. |
| Account was enriched but the rep says "the research is outdated, redo it" | Do **not** override the filter automatically. Escalate to Mark Hyun (mark.hyun@moxo.com) to reset the flag in HubSpot. Do not re-research without the flag being cleared. |
| Account was enriched in the current session (in-memory) | Skip — even if the HubSpot flag hasn't been written back yet (it gets written in Step 7), treat as already enriched for the rest of the session. |

**Example:**
> InvestCo shows `AI (Claude) Enriched?` = Yes → exclude, log: "Excluded — Already Enriched."

---

## Rule 5 — Open Deal

**What it means:** If the company has any associated deal in HubSpot with a stage that is NOT "Closed Won" or "Closed Lost," the account is actively being worked by sales. Do not build outreach for it.

**Open deal stages include (not exhaustive):** Appointment Scheduled, Qualified to Buy, Presentation Scheduled, Decision Maker Bought-In, Contract Sent, Pending, In Progress, Demo Scheduled, Proposal Sent, Negotiation.

**Edge cases:**

| Scenario | Decision |
|----------|----------|
| Company has one Closed Won deal and one open deal | Exclude — any active open deal means skip. |
| Company has only Closed Lost deals | Include — no active pipeline. |
| Deal stage is blank or unrecognized | Treat as open — exclude to be safe. Log: "Excluded — Open Deal in Progress (stage unrecognized, treated as active)." |
| Company has no associated deals at all | Include — no deal history means no active pipeline. |

**Example:**
> Summit Financial has a deal in "Contract Sent" stage → exclude, log: "Excluded — Open Deal in Progress (Contract Sent)." Peak Advisors has two deals both in "Closed Won" → include.

---

## Rule 6 — M&A Target

**What it means:** Companies actively involved in an acquisition — either being acquired or in the process of acquiring another company — are unlikely to commit to new vendors during the evaluation period. Exclude them.

**What triggers this rule:**
- News of a pending acquisition or merger confirmed in a press release or reputable outlet
- Company issued a press release stating they are "exploring strategic alternatives" (common language for M&A activity)
- Company leadership publicly commented on an ongoing transaction

**Edge cases:**

| Scenario | Decision |
|----------|----------|
| M&A was announced but closed more than 6 months ago | Include — the company is now post-integration. Research the new combined entity. |
| Rumor of acquisition with no official statement | Include — rumors are not confirmed M&A. Note in Audit Log: "Possible M&A rumor — not confirmed. Included." |
| Company is acquiring a small unrelated entity (bolt-on acquisition) | Use judgment. If the acquisition is small and unlikely to disrupt operations, include. If it is a material acquisition that would dominate the company's attention, exclude. |
| Company was acquired and is now a subsidiary | Exclude — go-to-market decisions likely moved to the parent company. Log: "Excluded — M&A Target (now a subsidiary of [Parent Co])." |

**Example:**
> LexGroup Law announces it is being acquired by NationalLaw Partners → exclude, log: "Excluded — M&A Target (acquisition by NationalLaw Partners announced March 2026)." RealtyCo announces acquisition of a small 5-person prop-tech startup → include (bolt-on, low disruption risk).

---

## Rule 7 — Target Industries

**In-scope industries:**
Real Estate | Financial Services | Banking | Computer Software | IT and Services | Insurance | Manufacturing | Law Practice | Legal Services | Business Services | Investment Management | Investment Banking | Commercial Real Estate

**Edge cases:**

| Scenario | Decision |
|----------|----------|
| Industry field is blank in HubSpot | Do not exclude. Check the company website or LinkedIn. If the company clearly fits an in-scope vertical, include and note the inferred industry in the Audit Log. |
| Industry shows a close variant not on the list (e.g., "Mortgage" or "CPA / Accounting") | Apply judgment: Mortgage → maps to Financial Services (include). Accounting / CPA → maps to Business Services or Financial Services (include). When in doubt, include and note the mapping. |
| Industry is "Staffing and Recruiting" | Exclude — not on the target list. |
| Industry is "Healthcare" | Exclude — not on the target list unless the company is also clearly providing financial/legal/insurance services as their primary product. |
| Industry is "Retail" | Exclude. |
| Industry is "SaaS" or "Software" | Maps to Computer Software — include. |
| Industry is "Consulting" | Maps to Business Services or Professional Services — include. |

**Example:**
> HubSpot shows "Accounting" → include, map to Business Services. HubSpot shows "Hospital & Health Care" → exclude, log: "Excluded — Industry Not in ICP (Healthcare)."

---

## Rule 8 — Negative Keywords

**Skip if company name, industry, or domain contains any of:**
`university` | `school` | `college` | `non-profit` | `nonprofit` | `government` | `defense` | `weapons` | `.edu` | `school district`

**Case-insensitive match. Partial match counts.**

**Edge cases:**

| Scenario | Decision |
|----------|----------|
| Company name is "Defense Analytics LLC" but sells commercial SaaS | Exclude — the keyword match takes precedence regardless of what they sell. |
| Domain is `.edu` but company is a for-profit education technology company | Exclude — .edu domain is always excluded. |
| Company name contains "school" as a non-education context (e.g., "Old School Investments LLC") | Use judgment. If the company is clearly a financial services firm with "old school" as branding flavor, include. If any ambiguity, exclude to be safe. |
| Company name contains "College" as a proper name (e.g., "College Park Properties") | Exclude — the keyword match is strict. If you are confident it is not an educational institution, note in Audit Log: "Keyword match 'college' — confirmed commercial real estate firm — included." |
| Nonprofit status is known from research but not in company name/domain | Exclude — IRS 501(c) status or clear nonprofit mission disqualifies the company regardless of whether the keyword appears. |

**Example:**
> "Westfield Community College Foundation" → exclude, log: "Excluded — Negative Keyword Match (college)." "Government IT Solutions Inc" → exclude, log: "Excluded — Negative Keyword Match (government)."

---

## Ghost Company Rule

If web search yields **zero results from the last 12 months** for a company, it is a ghost — either defunct, extremely small/private with no press presence, or misnamed in HubSpot.

**Action:** Skip the company. Log in Audit Log as: `"Ghost — No Recent News"`. Pull the next eligible account from the filtered pool to replace it.

**Do not count ghost companies against the 5-account batch target.** Always aim to deliver 5 fully researched accounts per batch.

---

## Shielded Exec Rule

If a specific executive's name cannot be confirmed via web search results or LinkedIn — do not invent or guess it.

**Format:** `[Title] — [Name Unverified]`

**Example:** `Chief Operating Officer — Name Unverified`

This rule applies everywhere a name would appear: Target Prospects, email salutations, and HubSpot notes. Never write "John Smith, COO" unless you have verified the name from a source.

---

## Batch Size & Pool Exhaustion

- Target batch size: **5 accounts per run**
- If fewer than 5 qualified accounts exist in the filtered pool, deliver however many qualify (3, 2, 1) and tell the rep explicitly how many were found and why the pool ran short
- If **zero** qualified accounts remain, stop and tell the rep with a breakdown of exclusion reasons and suggested remedies (expand headcount range, check for newly assigned accounts, confirm filters with manager)
- Ghost companies do not count toward the 5 — always replace them with the next eligible account

---

## Audit Log Standards

Every excluded account needs a specific, professional exclusion note — Mark Hyun reads these.

**Good:** `"Excluded — Open Deal in Progress (Contract Sent stage, deal created 2026-01-14)"`
**Bad:** `"Excluded — has a deal"`

**Good:** `"Excluded — Outside Geography (HQ in United Kingdom)"`
**Bad:** `"Excluded — wrong country"`

**Good:** `"Excluded — Negative Keyword Match (domain: stateuniversity.edu)"`
**Bad:** `"Excluded — education"`

For researched accounts, the Audit Log entry should include a one-sentence "Why We Chose This Account" and a one-sentence "Business Impact" grounded in the trigger event — not generic statements.

**Good:** `"Why Selected: New COO at Acme Financial signals a mandate to modernize client onboarding processes."`
**Bad:** `"Why Selected: Good fit for Moxo."`
