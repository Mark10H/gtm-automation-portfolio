# ADR-001: Prod Prospecting & Outreach Pipeline Architecture

**Status:** Accepted
**Date:** 2026-04-12
**Deciders:** Mark Hyun (BDR Ops Lead)

---

## Context

Moxo's BDR team needs a repeatable, auditable outbound pipeline that takes a rep's HubSpot territory, filters it against strict SOP rules, enriches accounts with dual-signal intelligence (ZoomInfo + live web research), prospects ICP contacts, generates personalized 4-step email sequences grounded in Moxo's KB, and enrolls everything into HubSpot — all in a single skill invocation. The pipeline must be transparent (every exclusion has a logged reason), resumable across sessions, and safe against data quality failures (ghost companies, missing contacts, spam-trigger emails).

### Forces at Play

- **Scale vs. quality:** Reps need 5 deeply researched accounts per batch, not 50 shallow ones. The pipeline must score and rank aggressively.
- **Data trust:** HubSpot data is often incomplete (blank headcount, missing country). ZoomInfo may be unavailable. The pipeline must degrade gracefully.
- **Audit accountability:** Mark reads the audit logs. Every exclusion and selection must have a specific, defensible reason — not generic labels.
- **KB fidelity:** All Moxo positioning, objection handling, and stats must originate from `moxo-kb.md`. The system must never hallucinate product capabilities.
- **Session fragility:** Long pipelines can time out or lose context. Checkpoint/resume is mandatory.

---

## Decision

Implement an 8-step orchestrated pipeline with dual-signal enrichment, multi-gate quality controls, and a 7-sheet Excel deliverable. The pipeline is implemented as a single Cowork skill (`SKILL.md`) backed by 8 Python scripts and 3 reference files, deployed as the **production** skill (`prod-prospecting-and-outreach`), separate from the dev/testing skill (`prospecting-and-outreach`).

---

## Architecture Overview

### Component Map

```
prod-prospecting-and-outreach/
├── SKILL.md                          # Orchestrator — 8-step workflow instructions
├── references/
│   ├── moxo-kb.md                    # Authoritative product knowledge base
│   ├── rep-config.json               # Per-rep config (dual_owner_reps list)
│   └── sop-rules.md                  # 8 SOP filtering rules + audit standards
└── scripts/
    ├── filter_accounts.py            # Step 2: SOP rule engine
    ├── score_accounts.py             # Step 1.5: Dual-signal scoring (ZI + web)
    ├── contact_enrichment.py         # Step 5: Contact triage & email inference
    ├── email_quality_gate.py         # Step 6.5: Spam/quality blocker
    ├── build_excel.py                # Step 7: 7-sheet workbook generator
    ├── sequence_export.py            # Step 7: Cadence + HubSpot import files
    ├── checkpoint_manager.py         # Step 8: Session state persistence
    └── territory_health.py           # Step 8: Territory analytics dashboard
```

### Data Flow

```
[HubSpot MCP]                         [ZoomInfo MCP]
     │                                      │
     ▼                                      ▼
Step 1: Pull rep's companies ──────► Step 1.5: Enrich intent + scoops
     │                                      │
     ▼                                      ▼
Step 2: SOP filter (8 rules) ◄──── Step 1.5B: Live web research
     │                                      │
     ▼                                      ▼
Qualified pool (≤100) ──────────► Step 1.5C: Score & rank (top 10, ≥70pts)
                                            │
                                            ▼
                                   Step 3: Deep web research per account
                                            │
                                            ▼
                                   Step 4: Build intelligence (7 fields)
                                            │  ← Rep approval gate
                                            │  ← Set ai_claude_enriched = Yes
                                            ▼
                                   Step 4.5: ZoomInfo contact pull (≤10/co)
                                            │
                                            ▼
                                   Step 5: Contact triage (FULL/EMAIL/PHONE/SHELL)
                                            │
                                            ▼
                                   Step 5.5: Enrich missing data (ZI + web fallback)
                                            │
                                            ▼
                                   Step 6: Generate 4-email sequences (KB-grounded)
                                            │
                                            ▼
                                   Step 6.5: Email quality gate (block/warn/pass)
                                            │  ← Rep approval gate
                                            ▼
                                   Step 7: Build Excel + HubSpot enrollment
                                            │
                                            ▼
                                   Step 8: Checkpoint + territory health
```

---

## Integration Design

### External Dependencies

| Integration | MCP Tools Used | Failure Mode | Fallback |
|-------------|---------------|--------------|----------|
| **HubSpot** | `search_owners`, company queries, `manage_crm_objects` | Owner lookup fails | Rep confirms email directly |
| **ZoomInfo (Intent)** | `enrich_intent` (9 topic keywords) | MCP unavailable | Web-only scoring, normalize to 100-pt scale |
| **ZoomInfo (Scoops)** | `enrich_scoops` (exec moves, funding, M&A) | MCP unavailable | Web-only scoring, M&A catch moves to web research |
| **ZoomInfo (Contacts)** | `search_contacts` (Tier 1/2 title targeting) | No results | Expand title criteria; log "No ZI contacts found" |
| **ZoomInfo (Enrich)** | `enrich_contacts` (validate email/phone) | Validation fails | Web search fallback, LinkedIn-only outreach |
| **WebSearch** | Company news, trigger events, contact email fallback | Zero results | Ghost Company Rule — skip, pull next eligible |
| **Gmail** | Implied for sequence enrollment | Not directly called | HubSpot sequence enrollment via MCP |

### HubSpot Field Dependencies

The pipeline reads and writes specific HubSpot fields. Changes to these field names or types will break the pipeline:

- **Read:** Company Owner, Company Owner B, Number of Employees, Industry, Country/Region, Domain Name, Is an Active Customer?, AI (Claude) Enriched, Deal Stages
- **Write:** AI (Claude) Enriched (Yes/No dropdown), Contact records (name, title, email, phone, LinkedIn, industry), Tasks (call + LinkedIn follow-up), Sequence enrollment

---

## Scoring Model

### Composite Score (max 100 points)

| Component | Max Points | Source | Recency Weighting |
|-----------|-----------|--------|-------------------|
| ZoomInfo intent signals | 30 | `enrich_intent` — 9 topic keywords | N/A (current snapshot) |
| ZoomInfo scoops | 25 | `enrich_scoops` — exec moves, funding, M&A | 5 tiers: ≤30d → 91–180d → >365d |
| Web research (Cat A: News) | 15 | WebSearch — company announcements | 3 tiers: ≤90d, 91–365d, >365d |
| Web research (Cat B: Digital) | 10 | WebSearch — AI/digital transformation signals | Same |
| Web research (Cat C: Ops Gap) | 10 | WebSearch — operational challenges | Same + KB alignment check |
| Web research (Cat D: Growth) | 10 | WebSearch — churn/retention signals | Same |

**Threshold:** ≥70 qualifies. <70 hard-disqualified.
**Layoff penalty:** −10 pts default; −5 if restructuring language; 0 if ops/c-suite hiring detected alongside.
**M&A detection:** Immediate exclusion + audit log entry (even during scoring, not just SOP filter).

---

## Quality Gates

The pipeline has **4 quality gates** that prevent bad data from propagating:

1. **SOP Filter (Step 2)** — 8 rules: headcount, geography, active customer, already enriched, open deal, M&A, target industry, negative keywords. Every exclusion logged with specific reason.

2. **Enrichment Score Threshold (Step 1.5C)** — Hard floor at 70/100. Accounts below are not surfaced to the rep.

3. **Rep Approval (Step 4)** — Human-in-the-loop checkpoint. Rep reviews account intelligence before contact prospecting begins. Enriched flag set in HubSpot only after approval.

4. **Email Quality Gate (Step 6.5)** — Automated scan for banned openers, spam triggers, placeholders, forbidden words, render-risk characters. BLOCK/WARN/PASS status. Rep approves before enrollment.

---

## Known Risks

### P0 — Pipeline-Breaking

| Risk | Impact | Mitigation | Status |
|------|--------|------------|--------|
| HubSpot enriched flag write fails silently | Accounts get re-researched in next batch, wasting quota | Retry in Step 7 final update; log warning if first write fails | ⚠️ Implemented but no alerting |
| ZoomInfo MCP goes down mid-batch | Scoring breaks — ZI sub-score returns 0 for remaining accounts | Fall back to web-only scoring with normalized threshold | ✅ Implemented |
| Checkpoint corruption | Session can't resume; rep loses partial progress | Schema versioning (v1.0); merge rules documented; stale checkpoint warning at 24h | ✅ Implemented |
| Ghost company in scoring pool | Wasted research slot; fewer than 5 accounts delivered | Ghost Company Rule skips + pulls next eligible; doesn't count against batch target | ✅ Implemented |

### P1 — Quality Degradation

| Risk | Impact | Mitigation | Status |
|------|--------|------------|--------|
| KB drift — moxo-kb.md falls out of date | Email sequences cite stale stats, wrong capabilities | Manual KB updates required; no automated freshness check | ❌ No automation |
| Industry alias mismatch | Accounts excluded that should qualify (e.g., new alias not mapped) | Alias table in filter_accounts.py + territory_health.py must stay in sync | ⚠️ Manual sync required |
| Email pattern inference sends to wrong address | Bounce, spam flag, or delivery to wrong person | High-confidence threshold + mandatory ZI validation before send | ✅ Implemented |
| Contact geo tokens outdated | CA provinces or US states list incomplete → valid contacts excluded | Static lists in contact_enrichment.py; need periodic review | ⚠️ Static lists |

### P2 — Operational

| Risk | Impact | Mitigation | Status |
|------|--------|------------|--------|
| Territory exhaustion | Rep runs out of eligible accounts; batch target unreachable | Territory health dashboard warns; suggestion to expand filters or check assignments | ✅ Implemented |
| Batch size assumptions | 100-account scoring pool may be too small for reps with large territories | Configurable via batch_limit parameter | ✅ Configurable |
| openpyxl dependency missing | Excel build fails | Skill instructions note `pip install openpyxl --break-system-packages` | ⚠️ Runtime check only |

---

## TODOs & Improvement Backlog

### High Priority

- [ ] **Automated KB freshness check** — Add a timestamp or version hash to `moxo-kb.md` and warn if >30 days old when skill runs
- [ ] **Industry alias sync script** — Single source of truth for industry aliases shared between `filter_accounts.py` and `territory_health.py`
- [ ] **Enriched flag write retry alerting** — Surface failed writes to the rep in the audit log, not just internal logs
- [ ] **Contact geo token refresh** — Script to pull current ISO codes for US states and CA provinces

### Medium Priority

- [ ] **Scoring model tuning** — Track conversion rates by enrichment score band to validate the 70-point threshold empirically
- [ ] **Email A/B metrics** — Track open/reply rates per email step (E1–E4) to inform template improvements
- [ ] **Dual-owner rep auto-detection** — Instead of manual `rep-config.json` entries, detect Company Owner B field presence automatically
- [ ] **Parallel account research** — Step 3 researches accounts sequentially; could parallelize with subagents for speed

### Low Priority / Nice-to-Have

- [ ] **Competitor mention detection** — Flag competitor names (Salesforce, DocuSign, PandaDoc) in web research for talk track selection
- [ ] **LinkedIn signal integration** — Pull job postings, recent posts for additional trigger signals
- [ ] **Multi-rep batch mode** — Run batches for multiple reps in a single session
- [ ] **Historical batch comparison** — Compare this batch's scores/triggers to previous batches for trend analysis

---

## Change Log

| Date | Change | Reason | Author |
|------|--------|--------|--------|
| 2026-04-12 | ADR created | Document architecture, risks, and backlog for the prospecting-and-outreach skill | Mark Hyun |
| 2026-04-12 | Skill duplicated as prod-prospecting-and-outreach | Separate production skill from dev/testing skill to enable safe iteration on prospecting-and-outreach without affecting live BDR workflows | Mark Hyun |

---

## Consequences

### What becomes easier

- **Onboarding new BDR ops contributors** — This document provides a complete map of the pipeline, its dependencies, and its failure modes.
- **Debugging pipeline failures** — The risk table and integration map point directly to the component and fallback for each failure type.
- **Prioritizing improvements** — The TODO backlog is categorized by impact so the team can plan sprints around it.
- **Auditing changes** — The change log tracks what was modified and why, preventing "mystery changes" that break downstream steps.

### What becomes harder

- **Nothing significant** — This is a documentation artifact, not an architectural change. The only overhead is keeping this document updated as the skill evolves.

### What we'll need to revisit

- **Scoring thresholds** — The 70-point floor is based on initial calibration. Revisit after 50+ batches with conversion data.
- **ZoomInfo topic keywords** — The 9 intent topics were chosen at launch. New Moxo use cases may require additions.
- **Email cadence blueprints** — CADENCE_FULL (7-step) and EMAIL_ONLY (4-step) should be validated against actual reply-rate data.
- **This ADR itself** — Review quarterly or when any P0/P1 risk status changes.
