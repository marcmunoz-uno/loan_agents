# loan_agents

Flask microservice for Tranchi.ai's lending stack. Three agent surfaces:

- **Tranchi - Loan Officer** (`/api/loan/*`) — borrower-facing: prequal scoring, application state machine, Arive/Zapier two-way LOS integration, lender routing, **auto-generated pre-qualification letters** (PDF + email + audit log).
- **Tranchi - Loan Processor** (`/api/processor/*`) — internal pre-underwriting: scores files against `lender_guidelines/`, generates conditions, drafts credit memos, fires Zapier into Arive when clean.
- **Intake pipeline** (`/api/intake/*`) — S3 presigned upload + Claude vision OCR + per-product completeness checks + **auto-fires the prequal letter when the checklist completes**.

Carved out of `tranchi-deal-flow-agents`. Deployed standalone on Render at **https://loan-agents.onrender.com** (`srv-d85vjqjrjlhs73abuu1g`). Talks to the rest of the stack via the same `Authorization: Bearer <TRANCHI_API_SECRET>` header.

---

## Capabilities

| Capability | Endpoint(s) | Status |
|---|---|---|
| Borrower **prequal scoring** (DSCR / LTV / fit-score, suggested product, strengths, concerns, next steps) | `POST /api/loan/prequal` | ✅ live |
| **Application** state machine (NEW → DOCS → UNDERWRITING → APPROVED/DECLINED/FUNDED) | `POST /api/loan/application`, `GET /api/loan/application/<id>` | ✅ live |
| **Conversational chat** with Loan Officer and Loan Processor personas (Claude on the other end) | `POST /api/loan/chat`, `POST /api/processor/chat` | ✅ live |
| **Pre-underwriting** — rank 9 lender programs, generate PTSU/PTD/PTC conditions, draft credit memo | `POST /api/processor/pre-underwrite/<app_id>` | ✅ live |
| **Lender guideline library** (9 products across 6 lenders: Lima One, Kiavi, New Silver, LendingOne, Roc, Anchor) | `GET /api/processor/guidelines`, `GET /api/processor/guidelines/<lender_id>`, `POST /api/processor/guidelines/check` | ✅ live |
| **Arive/Zapier two-way LOS bridge** — outbound events (prequal_created, app_submitted, docs_uploaded, ready_for_underwriting, lender_routed, approved, declined, funded), HMAC-verified inbound webhooks | `POST /api/loan/webhook/arive-update`, `fire_zap()` | ✅ live |
| **Intake doc upload** — S3 presigned PUT, server-side HEAD verify on confirm | `POST /api/intake/upload/presign`, `POST /api/intake/upload/confirm` | ✅ live (needs `AWS_S3_BUCKET` to actually accept uploads) |
| **OCR document classifier** — Claude vision; classifies into 15 doc types (w2 / bank_stmt / rent_roll / tax_return / purchase_contract / appraisal / lease_agreement / id_government / …) + extracts key fields per type | `POST /api/intake/upload/<doc_id>/classify` | ✅ live |
| **Per-product completeness gap report** | `GET /api/intake/applications/<app_id>/completeness?product=dscr` | ✅ live |
| **Auto-generated pre-qualification letters** — sums liquidity from classified bank statements, computes a two-number max-PP range (conservative + stretch), renders the firm's letterhead PDF, emails it via Zapier MCP → Gmail with the PDF attached, writes an audit row | `POST /api/loan/prequal-letter/<prequal_id>`, `GET /api/loan/prequal-letter/<letter_id>`, `GET /api/loan/prequal-letter/<letter_id>/pdf?token=<hmac>&exp=<unix>` | ✅ live |
| **Autonomous classify → letter trigger** — after a successful `/classify`, if the DSCR checklist is now complete and no letter went out in the last 24h, the system auto-renders + auto-sends the prequal letter. No human in the loop. | side-effect of `POST /api/intake/upload/<doc_id>/classify` | ✅ live |
| **Typeform auto-prequal** — every submission to `prequal.typeform.com/qualification` is ingested, soft-prequal scored (credit-pull gate + ≥620 credit gate + doc completeness), then the AI Loan Officer composes and sends a personalised result email via Zapier MCP → Gmail. Idempotent on Typeform's response token. HMAC-verified. | `POST /api/loan/webhook/typeform-submit` | ✅ live |
| **Self-hosted attachment endpoint** — Zapier-fetchable, HMAC-tokenized, 14-day TTL. Regenerates the PDF deterministically from the audit row, so we never have to persist PDF bytes. | `GET /api/loan/prequal-letter/<letter_id>/pdf?token=<hmac>&exp=<unix>` | ✅ live |

---

## Local Quick-Start

```bash
git clone git@github.com:marcmunoz-uno/loan_agents.git
cd loan_agents
./ops/run_local.sh
```

Creates a venv, installs deps, seeds sample data, starts the server at `http://localhost:5010`.

**Smoke test against the deployed service:**

```bash
TRANCHI=<your TRANCHI_API_SECRET>

# Health
curl https://loan-agents.onrender.com/health

# Prequal — score a borrower + property
curl -X POST https://loan-agents.onrender.com/api/loan/prequal \
  -H "Authorization: Bearer $TRANCHI" -H "Content-Type: application/json" \
  -d '{"borrower":{"user_id":"usr_marc","name":"Marc","email":"marc@munoz.ltd","credit_score":740,"liquidity":50000,"properties_owned":3,"desired_loan_amount":80000,"down_payment_pct":20},"property":{"address":"4521 Oak Ln Detroit MI","property_type":"single_family","purchase_price":100000,"monthly_rent":1300,"annual_taxes":2400,"annual_insurance":1200}}'

# Pre-underwrite the seeded application
curl -X POST https://loan-agents.onrender.com/api/processor/pre-underwrite/app_seed_marc_001 \
  -H "Authorization: Bearer $TRANCHI"

# Generate + auto-send a pre-qualification letter (PDF attached via Zapier MCP → Gmail)
curl -X POST https://loan-agents.onrender.com/api/loan/prequal-letter/<prequal_id> \
  -H "Authorization: Bearer $TRANCHI" -H "Content-Type: application/json" -d '{}'

# Lender guideline matrix
curl -H "Authorization: Bearer $TRANCHI" https://loan-agents.onrender.com/api/processor/guidelines
```

---

## End-to-End Autonomous Flow

```
Borrower lands in the Tranchi UI (or hits Typeform)
   ↓
POST /api/loan/prequal             ←  scored, stored
   ↓
POST /api/loan/application         ←  app row created in APP_DOCS_PENDING
   ↓
Borrower uploads bank stmts / rent roll / purchase contract
  via /api/intake/upload/presign + S3 PUT + /api/intake/upload/confirm
   ↓
POST /api/intake/upload/<doc_id>/classify   ← Claude vision tags each doc
   ↓
On the classification that flips DSCR completeness to True:
   ├── liquidity summed from classified bank_stmts
   ├── max_pp range computed (conservative + stretch params)
   ├── PDF rendered (Munoz, Ghezlan & Co. letterhead)
   ├── PDF served at HMAC-tokenized /pdf URL
   ├── Zapier MCP → Gmail Send (PDF attached, 24h dedup)
   └── Audit row written to prequal_letters
   ↓
POST /api/processor/pre-underwrite/<app_id>  ← runs FICO/LTV/DSCR/PITI/cashflow,
                                               ranks 9 lender programs, drafts
                                               conditions + credit memo
   ↓ (if clean)
Zapier → Arive LOS as "Ready for Submission" with the credit memo attached
   ↓
Underwriter decides → Arive fires webhook → POST /api/loan/webhook/arive-update
   ↓
Application advances: UNDERWRITING → APPROVED / CONDITIONS / DECLINED → FUNDED
```

---

## Architecture

```
Tranchi Web App / Typeform / chat UI
        │  POST /api/...
        │  Authorization: Bearer TRANCHI_API_SECRET
        ▼
loan-agents.onrender.com (Flask + gunicorn)
   ├── Tranchi - Loan Officer     /api/loan/*
   │     ├── prequal              (DSCR / LTV / fit-score)
   │     ├── application          (state machine)
   │     ├── prequal-letter       (PDF + email)
   │     └── chat                 (Claude)
   ├── Tranchi - Loan Processor   /api/processor/*
   │     ├── pre-underwrite       (lender_fit, conditions, credit memo)
   │     ├── guidelines           (9 products, 6 lenders)
   │     └── chat                 (Claude)
   └── Intake                     /api/intake/*
         ├── upload (S3 presign)
         ├── classify (Claude vision)
         └── completeness         → autonomously fires prequal-letter

         ▼ external services ▼

Anthropic API ← (chat, vision OCR, credit memos)
S3            ← (intake doc storage, optional letter PDF host)
Zapier MCP    → Gmail Send (prequal letter delivery)
Zapier Hooks  → Arive LOS  (LOS sync — prequal_created / approved / funded / …)
```

Everything is one Flask service today. Each agent surface is a Blueprint registered in `app.py`.

---

## Deployment (Render)

Already live at https://loan-agents.onrender.com. To redeploy or fork:

1. Connect the repo in Render — auto-reads `render.yaml`.
2. Set the `sync: false` env vars:
   - `ANTHROPIC_API_KEY`
   - `TRANCHI_API_SECRET` (shared with `tranchi-outbound-agent`)
   - `OPENAI_API_KEY` (optional fallback)
   - `ZAPIER_MCP_ENDPOINT` (full URL; Zapier's modern format is `…/api/v1/connect?token=…`)
   - `ZAPIER_MCP_API_KEY` (optional — skipped automatically when the endpoint URL already carries auth)
   - `AWS_S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (only if you want real intake uploads + S3-hosted PDF attachments)
3. The self-hosted PDF endpoint serves attachments without S3 — set `PUBLIC_BASE_URL` to the service's public URL so the Zapier-fetchable URLs resolve.

See `ops/deploy_render.md` for the full env-var checklist.

---

## Repo Structure

```
app.py                              # Flask entry — registers loan_bp + intake_bp + processor_bp
shared/
  auth.py                           # Bearer token decorator
  db.py                             # SQLite layer + idempotent schema-patch loop
  llm.py                            # Anthropic primary (chat + chat_with_tools + chat_with_vision),
                                    # OpenAI fallback for chat()
  s3_client.py                      # boto3 wrapper — presigned PUT/GET, server-side put_object_bytes
  schemas.py                        # Pydantic models (BorrowerProfile, PropertyProfile, etc.)
  webhooks.py                       # HMAC-SHA256 verify
  zapier_mcp.py                     # Streamable-HTTP MCP client (auto-detects URL-embedded auth)
  migrations/
    001_initial.sql                 # loan_prequals, loan_applications, loan_documents, audit_log
    002_loan_processor.sql          # pre_underwriting_reports
    003_intake.sql                  # intake_documents (S3 keys, classification results, lifecycle)
    004_prequal_letters.sql         # audit log of every generated letter

loan_officer/                       # /api/loan/*
  routes.py                         # All loan endpoints + the public /pdf endpoint
  prequal.py                        # DSCR/LTV/fit-score math
  lender_router.py                  # Product scoring + lender matching
  lender_partners.py                # 6 lender partner configs
  document_collector.py             # Per-product required-docs checklist
  workflows.py                      # State machine + audit log
  arive_zapier.py                   # Outbound webhook fan-out + Arive field mapping
  prequal_letter.py                 # max-PP math, reportlab PDF render, Zapier-MCP send,
                                    # HMAC token sign/verify, regenerate-from-audit-row
  system_prompt.md                  # Loan Officer persona
  intake/                           # /api/intake/*
    upload.py                       # presign / confirm / status / attach helpers
    ocr_classifier.py               # Claude vision wrapper, 15-doc-type taxonomy
    completeness.py                 # Per-product checklists + gap reports
    routes.py                       # Intake endpoints + the autonomous classify→letter hook
    chatbot.py                      # (stubbed) conversational data-collection front door
  tests/                            # 100+ cases — workflow, prequal letter, intake routes, etc.

loan_processor/                     # /api/processor/*
  routes.py                         # 6 processor endpoints
  pre_underwriting.py               # Pre-UW engine + PreUnderwritingReport
  guideline_engine.py               # Loads + queries lender_guidelines/
  condition_generator.py            # Conditions per product + lender
  credit_memo.py                    # LLM-drafted credit memos
  system_prompt.md                  # Loan Processor persona
  tests/test_pre_underwriting.py

lender_guidelines/                  # 9 product specs in markdown + machine-readable index
  guidelines_index.json
  lima_one_dscr.md           kiavi_dscr.md            new_silver_fix_flip.md
  lima_one_fix_flip.md       kiavi_brrrr.md           lendingone_dscr.md
  roc_capital_dscr.md        roc_capital_brrrr.md     anchor_loans_fix_flip.md

ops/
  seed_data.py                      # Sample borrower + application + pre-UW report
  run_local.sh                      # One-command local dev boot
  deploy_render.md                  # Render deployment guide
```

---

## Tests

```bash
ANTHROPIC_API_KEY="" OPENAI_API_KEY="" python3 -m pytest -q
```

159 cases, all pass without network. Suite covers:

- Workflow state machine + audit log
- Prequal math (DSCR, LTV, fit-score)
- Lender router + product matching
- Arive/Zapier outbound (mocked)
- Intake upload, confirm, list, attach (mocked boto3)
- Claude vision classifier — happy path, JSON-parse robustness, partial-failure batch
- Per-product completeness gap detection
- Prequal letter math (range monotonicity, $1k flooring, DSCR vs liquidity binding)
- HMAC token sign / verify / tamper / expire
- Self-hosted `/pdf` endpoint — valid token serves PDF, bad token = 403, missing letter = 404, no Bearer needed
- Autonomous classify → letter trigger — fires on completeness, no-op when incomplete, dedupes within 24h

---

## Known Limitations

| Limitation | Why | When to fix |
|---|---|---|
| Intake routes return 503 until `AWS_S3_BUCKET` is set | No S3 bucket provisioned yet on Render | Whenever you stand up the bucket |
| 6 lender APIs are `# TODO: integrate` stubs | Real integrations need partnership credentials | When each partner deal closes |
| Intake chatbot 5 methods are `NotImplementedError` stubs | Designed but not implemented | After the doc-pipeline path is exercised end-to-end |
| Working Capital persona lender selectors are placeholders | Per-lender form HTML drifts; never deployed for consumers | Before consumer rollout, verify each lender's live page |
