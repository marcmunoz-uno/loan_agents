# tranchi-deal-flow-agents

This repo builds the backends for two gaps found in the Tranchi.ai platform audit: the "Automated Funding System" (Finance page is a Typeform link, no backend) and the "Transaction Automation Layer" (transaction management is partially implemented). The two agents here — **AI Loan Officer** and **Transaction Coordinator** — are designed as a standalone Flask microservice that deploys alongside `tranchi-outbound-agent` on Render, or merges into the main Tranchi app later. Production codebases (`tranchi-outbound-agent`, `tranchi-ui-audit`, `county-portal-scraper`) are not touched by this repo.

The 10-agent lineup below represents the full deal-flow automation stack for a real estate investor platform — from finding a deal to getting it funded, inspected, titled, and closed. This repo scaffolds the first two. The other eight are documented in `ROADMAP.md` with complexity estimates, integration requirements, and suggested build order.

---

## Agent Lineup

| # | Agent | Status |
|---|-------|--------|
| 1 | **AI Loan Officer** | ✅ scaffolded |
| 2 | **Transaction Coordinator** | ✅ scaffolded |
| 3 | Title & Escrow Coordinator | ROADMAP |
| 4 | Inspection Coordinator | ROADMAP |
| 5 | Property Manager Sourcer | ROADMAP |
| 6 | Listing Agent Liaison | ROADMAP |
| 7 | Cash Buyer Recruiter | ROADMAP |
| 8 | Insurance Quote Bot | ROADMAP |
| 9 | Tax / 1031 Advisor | ROADMAP |
| 10 | Capital Raiser | ROADMAP |

---

## Local Quick-Start

```bash
git clone git@github.com:marcmunoz-uno/tranchi-deal-flow-agents.git
cd tranchi-deal-flow-agents
./ops/run_local.sh
```

The script creates a venv, installs deps, seeds sample data, and starts the server at `http://localhost:5010`.

**Smoke test:**
```bash
# Health
curl http://localhost:5010/health

# Pre-qualify a borrower (DSCR loan for Detroit rental)
curl -X POST http://localhost:5010/api/loan/prequal \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"borrower":{"user_id":"usr_marc","name":"Marc","credit_score":740,"liquidity":85000,"properties_owned":3,"desired_loan_amount":71250,"down_payment_pct":25},"property":{"address":"4521 Oak Ln Detroit MI","property_type":"single_family","purchase_price":95000,"monthly_rent":1200,"annual_taxes":2400,"annual_insurance":1200}}'

# Check seeded transaction
curl -H "Authorization: Bearer dev-secret-change-me" \
  http://localhost:5010/api/tx/tx_seed_marc_001
```

Set `ANTHROPIC_API_KEY` in `.env` to enable the `/chat` endpoints.

---

## Integration Map

```
Tranchi Web App (Express + tRPC + React)
  └─► tranchi-deal-flow-agents (this repo, :5010)
        ├── AI Loan Officer    /api/loan/*
        │     └─► Lender Partners (Lima One, Kiavi, New Silver, LendingOne, Roc, Anchor)
        └── TX Coordinator     /api/tx/*
              └─► tranchi-outbound-agent (Hope voice / iMessage)
                    └─► Title, Inspector, Insurance via outbound comms

County Portal Scraper (county-portal-scraper)
  └─► tranchi_mcp_server.py (9 tools — property data)
        └─► feeds into Loan Officer property scoring
```

**Auth:** All endpoints accept `Authorization: Bearer <TRANCHI_API_SECRET>` — same header as `tranchi-outbound-agent`. One config change to wire into the main app.

**Webhook:** Lender partners push status updates to `POST /api/loan/webhook/lender-update`. Signed with HMAC SHA256 (`LENDER_WEBHOOK_SECRET`).

---

## Deployment

See `ops/deploy_render.md` for full Render deployment steps and env var checklist.

```bash
# render.yaml is preconfigured — just connect the repo in Render dashboard
# then set the env vars listed in .env.example
```

---

## Repo Structure

```
app.py                      # Flask entry point — registers both blueprints
shared/                     # Auth, DB, LLM, webhooks, Pydantic schemas
  migrations/001_initial.sql  # All tables (SQLite + MySQL compatible)
loan_officer/               # AI Loan Officer (blueprint: /api/loan/*)
  system_prompt.md          # Alex's full persona and rules
  workflows.py              # State machine: NEW → ... → FUNDED
  lender_router.py          # Product scoring + lender matching
  prequal.py                # DSCR/LTV/fit-score computation
  lender_partners.py        # 6 lender partner configs
  document_collector.py     # Required docs per product
  routes.py                 # 8 Flask endpoints
  tests/test_workflow.py    # State machine + prequal + router tests
tx_coordinator/             # Transaction Coordinator (blueprint: /api/tx/*)
  system_prompt.md          # Sam's persona and escalation rules
  timeline.py               # 16-milestone timeline generator
  deadline_engine.py        # Contingency deadline tracking + alerting
  parties.py                # Party management
  document_vault.py         # Doc checklist + status
  communication_hub.py      # All comms across parties
  routes.py                 # 8 Flask endpoints
  tests/test_timeline.py    # Timeline + deadline engine tests
ops/
  seed_data.py              # Sample borrower + property + transaction
  run_local.sh              # One-command local dev boot
  deploy_render.md          # Render deployment guide
```
