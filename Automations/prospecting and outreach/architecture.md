# Architecture — GTM Outbound Automation Pipeline

## Overview

This system automates the full outbound research and sequencing workflow for a BDR team. It replaces ~10 hours/week of manual work per rep by orchestrating account filtering, intent scoring, live research, contact enrichment, email generation, and CRM task creation in a single pipeline run.

The pipeline is AI-orchestrated — Claude (via the Anthropic API) handles all reasoning, research synthesis, and content generation. Python scripts handle the deterministic parts: filtering logic, scoring models, session state, and CRM writes.

---

## Data Flow

```
HubSpot (accounts)
       │
       ▼
  Step 1 — Pull & deduplicate accounts by rep owner
       │
       ▼
  Step 2 — ICP filter (headcount, geo, industry, keywords, active customer, open deals)
       │
       ▼
  Step 1.5 — Enrichment scoring
       │   ├── ZoomInfo MCP: intent signals (55% weight)
       │   └── Live web research: 4 trigger categories (45% weight)
       │         Threshold: score > 60 required
       │
       ▼
  Top 10 accounts (ranked by enrichment score)
       │
       ▼
  Step 3 — Live web research per account (trigger event verification)
       │
       ▼
  Step 4 — Account intelligence (6 fields: Process, Trigger, Pain, How Moxo Helps, Without Moxo, Outcome)
       │
       ▼
  Step 4.5 — ZoomInfo contact enrichment (ICP title filter, geo validation, email/phone)
       │
       ▼
  Step 5 — 4-step email sequences (account-level + contact-tailored)
       │
       ▼
  Rep approval gate (Steps 4 + 5 reviewed before any CRM writes)
       │
       ▼
  Step 7 — HubSpot account notes (account intelligence written to company record)
       │
       ▼
  Step 8 — HubSpot task creation
       │   ├── Full track: 4 email + 4 call tasks per contact
       │   ├── Email-only: 4 email tasks
       │   └── Phone-only: 5 call tasks
       │
       ▼
  Step 8F — Mark contacts + companies as enriched in HubSpot
       │
       ▼
  Excel audit output (7 sheets) + session checkpoint marked complete
```

---

## Key Design Decisions

### Why Claude as the orchestrator?

The pipeline needs to do things that are hard to script deterministically: judge whether a web search result is a real trigger event vs. thin noise, write emails that sound like a human researched the account, select the right proof point from a KB for a given industry. Claude handles all of that. The Python scripts handle the things that should be deterministic: filtering rules, scoring math, CRM API calls, file I/O.

### Composite enrichment scoring (Step 1.5)

Rather than randomly selecting 10 accounts, the pipeline ranks every filtered account before any research runs. ZoomInfo intent signals (55%) + live web research (45%) produce an enrichment score. Accounts scoring ≤ 60 are hard-disqualified. This means reps always work the highest-signal accounts first and no research time is wasted on cold accounts.

### Research integrity rules

The biggest risk in AI-assisted outbound is hallucinated signals. An AI that fabricates a "digital transformation initiative" for an account poisons every downstream deliverable — the intelligence summary, the emails, the call notes. The pipeline enforces strict sourcing rules at every research step: every signal must trace to a specific URL from a live web search, no general knowledge substitution, no inferring unstated details. Empty web_research is an acceptable and expected output.

### Checkpoint / resume system

Batches take 30-60 minutes to run across 10 accounts. Interruptions happen. The checkpoint manager writes session state after every major step boundary. If a run is interrupted, the rep types "resume" and the pipeline picks up from the last completed step — skipping re-pulls, re-scoring, and re-writing for anything already done.

### Rep approval gate (Step 6)

No CRM writes happen before the rep sees and approves the account intelligence and email sequences. This keeps humans in the loop on content quality before it goes into HubSpot and the rep's task queue. The pipeline generates, the rep approves, then CRM writes happen in batch.

### Contact track logic

Not every contact has both email and phone. ZoomInfo enrichment classifies each contact:
- `FULL`: email + phone found → 8 tasks (4 email + 4 call)
- `EMAIL_ONLY`: no phone → 4 email tasks
- `PHONE_ONLY`: no email → 5 call tasks (or LinkedIn-only if US/Canada not confirmed)
- `GEO_EXCLUDED`: contact outside US/Canada → skip

This prevents broken sequences and ensures every task in the rep's queue is actionable.

---

## External Integrations

| Integration | Purpose | Method |
|---|---|---|
| HubSpot | Account pulls, task creation, note writing, enrichment flags | HubSpot MCP |
| ZoomInfo | Intent signals, scoops, contact enrichment | ZoomInfo MCP |
| Web search | Live trigger event research | Claude tool use (web_search) |
| Claude API | Orchestration, research synthesis, email generation | Anthropic API |

---

## Session Directory Structure

Each run creates a session directory under `sessions/`:

```
sessions/20250401_143022/
├── accounts_raw.json          # raw HubSpot pull
├── filter_input.json          # filter script input
├── filter_output.json         # qualified + excluded accounts
├── intent_data.json           # ZoomInfo signals + web research per account
├── scored_accounts.json       # enrichment scores + top 10
├── account_intelligence.json  # Step 4 output per account
├── contacts_enriched.json     # Step 5.5 contact states
├── emails_generated.json      # Step 5 sequences per contact
├── checkpoint.json            # session state (resumable)
└── audit_log.xlsx             # 7-sheet Excel output
```

---

## Configuration

Copy `config.example.json` to `config.json`:

```json
{
  "hubspot_mcp_endpoint": "https://your-hubspot-mcp-endpoint",
  "zoominfo_mcp_endpoint": "https://your-zoominfo-mcp-endpoint",
  "anthropic_api_key": "sk-ant-...",
  "rep_email_domain": "yourcompany.com",
  "kb_path": "references/your-product-kb.md"
}
```

The knowledge base (`kb_path`) is a markdown file containing your product's value proposition, ICP, proof points, competitive positioning, and email language rules. The pipeline reads this before every run to ground all generated content.
