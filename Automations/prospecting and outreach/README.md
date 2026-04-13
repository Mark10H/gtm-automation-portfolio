# GTM Automation Portfolio

**Built by Mark Hyun — GTM Engineer**

This repo contains the production outbound automation system I built at Moxo to power a team of 6 BDRs. The system replaced ~10 hours/week of manual research and sequencing work per rep with an AI-orchestrated pipeline that runs from HubSpot pull to enrolled HubSpot tasks — without human intervention in the loop.

---

## What this system does

1. **Pulls target accounts** from HubSpot via MCP, filtered against strict ICP criteria
2. **Scores accounts** using a composite enrichment model — ZoomInfo intent signals (55%) + live web research (45%)
3. **Researches top 10 accounts** live using web search, grounded in verified trigger events only
4. **Builds account intelligence** — process, pain, Moxo fit, and business outcome per account
5. **Enriches contacts** via ZoomInfo — finds ICP contacts with email + phone, geo-validates, deduplicates
6. **Writes personalized 4-step email sequences** per contact, tailored to their role and trigger event
7. **Creates HubSpot tasks** for every contact — full track (8 tasks), email-only (4), or phone-only (5)
8. **Marks records as enriched** in HubSpot to prevent re-processing in future batches
9. **Outputs a 7-sheet Excel audit file** for manager review

---

## Stack

| Layer | Tools |
|---|---|
| AI Orchestration | Claude API (claude-sonnet), Gemini API |
| CRM | HubSpot MCP |
| Contact Enrichment | ZoomInfo MCP |
| Web Research | Live web search (Claude tool use) |
| Scripting | Python 3 |
| Output | openpyxl (Excel), JSON checkpoints |

---

## Repo structure

```
outbound_agent/
├── pipeline.py           # Main orchestration — runs the full workflow end to end
├── filter_accounts.py    # ICP filtering logic — headcount, geo, industry, keywords
├── score_accounts.py     # Composite enrichment scoring — ZI signals + web research
├── hubspot_client.py     # HubSpot MCP wrapper — pulls accounts, writes tasks/notes
├── zoominfo_client.py    # ZoomInfo MCP wrapper — intent signals, scoops, contact enrichment
├── email_generator.py    # 4-step sequence builder with contact-level tailoring
├── checkpoint_manager.py # Session state — write, read, validate, resume
└── build_excel.py        # 7-sheet Excel output builder

docs/
└── architecture.md       # How the pipeline fits together — data flow diagram + design decisions
```

---

## Results

- **~10 hrs/week** of manual research eliminated per rep across a team of 6
- Sequences grounded in verified live trigger events — no fabricated signals
- Hard-disqualifies accounts scoring ≤ 60 on enrichment score before any research runs
- Full session checkpointing — batches are resumable after interruption
- Manager-facing audit log with exclusion reasons, trigger categories, and task status per contact

---

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure your environment
cp config.example.json config.json
# Fill in: HubSpot MCP endpoint, ZoomInfo MCP endpoint, Claude API key

# Run a batch
python outbound_agent/pipeline.py --rep "First Last" --headcount "100-500"
```

---

## Notes

This repo contains the pipeline architecture and logic. Company-specific knowledge base files (product positioning, proof points, customer quotes) and live CRM credentials are not included. The system is designed to be adapted to any product with a comparable KB and HubSpot/ZoomInfo setup.

---

*Open to GTM Engineer roles. Let's connect → [linkedin.com/in/YOUR-SLUG](https://linkedin.com/in/YOUR-SLUG)*
