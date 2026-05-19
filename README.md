# loan_agents

Flask microservice for Tranchi.ai's lending stack. Two agents:

- **Tranchi - Loan Officer** (`/api/loan/*`) — borrower-facing: intake chat, prequal, application, doc collection, Arive/Zapier two-way LOS integration, lender routing.
- **Tranchi - Loan Processor** (`/api/processor/*`) — internal pre-underwriting: scores files against `lender_guidelines/`, generates conditions, drafts credit memos, fires Zapier into Arive when clean.

Carved out of `tranchi-deal-flow-agents`. Designed to deploy standalone on Render alongside `tranchi-outbound-agent`, or be wired into the main Tranchi app via the same `Authorization: Bearer <TRANCHI_API_SECRET>` header.

---

## Local Quick-Start

```bash
git clone git@github.com:marcmunoz-uno/loan_agents.git
cd loan_agents
./ops/run_local.sh
```

Creates a venv, installs deps, seeds sample data, starts the server at `http://localhost:5010`.

**Smoke test:**
```bash
# Health
curl http://localhost:5010/health

# Pre-qualify a borrower (DSCR loan for Detroit rental)
curl -X POST http://localhost:5010/api/loan/prequal \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"borrower":{"user_id":"usr_marc","name":"Marc","credit_score":740,"liquidity":85000,"properties_owned":3,"desired_loan_amount":71250,"down_payment_pct":25},"property":{"address":"4521 Oak Ln Detroit MI","property_type":"single_family","purchase_price":95000,"monthly_rent":1200,"annual_taxes":2400,"annual_insurance":1200}}'

# Run pre-underwriting on the seeded application
curl -X POST http://localhost:5010/api/processor/pre-underwrite/app_seed_marc_001 \
  -H "Authorization: Bearer dev-secret-change-me"

# Inspect lender guideline matrix
curl -H "Authorization: Bearer dev-secret-change-me" \
  http://localhost:5010/api/processor/guidelines
```

Set `ANTHROPIC_API_KEY` in `.env` to enable the `/chat` endpoints.

---

## Flow

```
Borrower
  │  chat
  ▼
Tranchi - Loan Officer        /api/loan/*
  ├── intake chatbot          (collects 6 prequal data points)
  ├── prequal                 (DSCR / LTV / fit-score)
  ├── application             (state machine: NEW → DOCS → READY)
  └── document_collector      (per-product doc checklist)
        │
        ▼
Tranchi - Loan Processor      /api/processor/*
  ├── guideline_engine        (loads lender_guidelines/*.md)
  ├── pre_underwriting        (FICO / LTV / DSCR / PITI / cashflow)
  ├── condition_generator     (PTSU / PTD / PTC by lender)
  └── credit_memo             (Claude-drafted)
        │
        ▼ (if clean — auto-fire Zapier)
Arive LOS  →  underwriter  →  webhook back to /api/loan/webhook/arive-update
        │
        ▼
Loan Officer advances status: UNDERWRITING → APPROVED / CONDITIONS / DECLINED
```

**Auth:** `Authorization: Bearer <TRANCHI_API_SECRET>` on all endpoints.
**Webhooks:** Lender + Arive callbacks signed with HMAC SHA256.

---

## Deployment

See `ops/deploy_render.md`. `render.yaml` is preconfigured — connect the repo in Render and set the env vars from `.env.example`.

---

## Repo Structure

```
app.py                              # Flask entry — registers loan_bp + processor_bp
shared/                             # Auth, DB, LLM, schemas, webhooks
  migrations/001_initial.sql        # loan_prequals, loan_applications, loan_documents
  migrations/002_loan_processor.sql # pre_underwriting_reports, intake_sessions
  browserbase.py                    # Browserbase co-pilot helper
  zapier_mcp.py                     # Zapier MCP transport
loan_officer/                       # /api/loan/*
  intake/                           # Stateful intake chatbot (chatbot.py et al.)
  arive_zapier.py                   # Arive two-way LOS integration
  workflows.py                      # Application state machine
  lender_router.py                  # Product scoring + lender matching
  prequal.py                        # DSCR/LTV/fit-score computation
  lender_partners.py                # 6 lender partner configs
  document_collector.py             # Required docs per product
  routes.py                         # Loan endpoints
  tests/                            # workflow + Arive integration tests
loan_processor/                     # /api/processor/*
  pre_underwriting.py               # Pre-UW engine + PreUnderwritingReport
  guideline_engine.py               # Loads + queries lender_guidelines/
  condition_generator.py            # Conditions per product + lender
  credit_memo.py                    # LLM-powered credit memos
  routes.py                         # Processor endpoints
  tests/test_pre_underwriting.py
lender_guidelines/                  # 9 products across 6 lenders
  guidelines_index.json             # Machine-readable matrix
  lima_one_dscr.md  kiavi_dscr.md  …
ops/
  seed_data.py                      # Sample borrower + application + pre-UW report
  run_local.sh                      # One-command local dev boot
  deploy_render.md                  # Render deployment guide
```
