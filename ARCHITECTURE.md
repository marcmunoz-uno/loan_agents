# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Tranchi Web App                                  │
│              (Express + tRPC + React, MySQL, 37 tables)             │
│   users | investor_profiles | placed_offers | cash_buyers | deals   │
└──────────────┬──────────────────────────────────────────────────────┘
               │ POST /api/loan/*  POST /api/processor/*  POST /api/tx/*
               │ Authorization: Bearer TRANCHI_API_SECRET
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│             tranchi-deal-flow-agents (this repo)                     │
│                        Flask · :5010                                 │
│  ┌────────────────────┐  ┌──────────────────┐  ┌─────────────────┐ │
│  │  AI Loan Officer   │  │  Loan Processor  │  │ TX Coordinator  │ │
│  │  (Alex)            │  │  (Casey)         │  │ (Sam)           │ │
│  │  /api/loan/*       │  │  /api/processor/*│  │ /api/tx/*       │ │
│  │                    │  │                  │  │                 │ │
│  │  prequal →         │◄─┤  pre_underwrite()│  │ PSA → timeline  │ │
│  │  application →     │  │  guideline_engine│  │ milestones →    │ │
│  │  docs → route →    │─►│  condition_gen() │  │ deadlines →     │ │
│  │  underwriting →    │  │  credit_memo()   │  │ parties →       │ │
│  │  funded            │  │  lender_fit rank │  │ closing day     │ │
│  └────────┬───────────┘  └────────┬─────────┘  └────────┬────────┘ │
│           │                       │                      │          │
│  ┌────────▼───────────────────────▼──────────────────────▼────────┐ │
│  │                         shared/                                 │ │
│  │  db.py · llm.py · auth.py · webhooks.py · schemas.py           │ │
│  │  migrations/001_initial.sql + 002_loan_processor.sql            │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────┬──────────────────────────┬──────────────────┬────────────────┘
       │                          │                  │
       │ Zapier webhooks          │ lender_guidelines│ tranchi-outbound-agent
       │ (outbound events)        │ (9 markdown docs │ (iMessage / voice)
       ▼                          │  + index.json)   ▼
┌─────────────────┐              └──────────────►  ┌──────────────────────┐
│  Zapier         │                                 │  tranchi-outbound-   │
│  prequal_created│                                 │  agent               │
│  app_submitted  │◄── Arive fires inbound webhook  │  POST /outreach/call │
│  docs_uploaded  │    POST /webhook/arive-update   │  POST /outreach/sms  │
│  ready_for_uw   │                                 └──────────────────────┘
│  lender_routed  │
│  approved       │
│  declined       │
│  funded         │
└────────┬────────┘
         │ Zapier action
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Arive LOS                                     │
│  Creates / updates loan records, sends disclosures, tracks pipeline  │
│  Fires webhooks on status change → Zapier → our /webhook/arive-update│
└─────────────────────────────────────────────────────────────────────┘

County Portal Scraper ──────────────────────────────────────────────►
  tranchi_mcp_server.py (9 tools)   →  property data feeds into prequal
```

---

## Sequence: Alex → Casey → Arive (New)

```
Borrower / Web App          Alex (Loan Officer)      Casey (Processor)         Arive LOS
     │                            │                        │                       │
     │  POST /api/loan/prequal    │                        │                       │
     │──────────────────────────► │                        │                       │
     │                            │ score_prequal()         │                       │
     │                            │ fire_zap(prequal_created)──────────────────────►│
     │  {prequal_id, score}       │                        │                       │ lead created
     │◄──────────────────────────│                        │                       │
     │                            │                        │                       │
     │  POST /api/loan/application│                        │                       │
     │──────────────────────────► │                        │                       │
     │                            │ fire_zap(app_submitted)────────────────────────►│
     │                            │                        │                       │ app updated
     │  POST .../documents        │                        │                       │
     │──────────────────────────► │                        │                       │
     │                            │ fire_zap(docs_uploaded)────────────────────────►│
     │                            │                        │                       │
     │  POST /api/processor/pre-underwrite/<id>            │                       │
     │──────────────────────────────────────────────────► │                       │
     │                            │           pre_underwrite()                      │
     │                            │           guideline_engine.match_lenders()      │
     │                            │           condition_generator.generate()        │
     │                            │           credit_memo.draft()  (LLM)           │
     │                            │                        │                       │
     │  overall_status = "clean"  │           if clean:    │                       │
     │                            │           fire_zap(ready_for_underwriting)─────►│
     │  {report, conditions,      │                        │                       │ status →
     │   credit_memo, lender_fit} │                        │                       │ "Ready for
     │◄──────────────────────────────────────────────────│                       │  Submission"
     │                            │                        │                       │
     │  POST .../route            │                        │                       │
     │──────────────────────────► │                        │                       │
     │                            │ fire_zap(lender_routed)────────────────────────►│
     │                            │                        │                       │
     │          [Arive UW decision — status change fires Zapier trigger]
     │                            │  POST /api/loan/webhook/arive-update            │
     │                            │◄─────────────────────────────────────────────── │
     │                            │ update state (CLOSING / APPROVED / DECLINED)    │
     │  status notification       │                        │                       │
     │◄──────────────────────────│                        │                       │
```

---

## Sequence: Investor Pre-Qualifies for a DSCR Loan

```
Investor (web app)          Tranchi Web App          Deal Flow Agents
     │                            │                        │
     │  "Get Financing" click     │                        │
     │──────────────────────────► │                        │
     │                            │  POST /api/loan/prequal│
     │                            │  {borrower, property}  │
     │                            │───────────────────────►│
     │                            │                        │ 1. suggest_product()
     │                            │                        │    → routes to DSCR
     │                            │                        │ 2. compute_dscr()
     │                            │                        │    dscr=1.18, ltv=0.65
     │                            │                        │ 3. score_prequal()
     │                            │                        │    score=78.5/100
     │                            │                        │ 4. persist to SQLite
     │                            │  {prequal_id, score,   │
     │                            │   product: "dscr",     │
     │                            │   monthly_est: $522,   │
     │                            │   next_steps: [...]}   │
     │                            │◄───────────────────────│
     │  Show pre-qual results     │                        │
     │◄───────────────────────────│                        │
     │                            │                        │
     │  "Start Application"       │                        │
     │──────────────────────────► │                        │
     │                            │  POST /api/loan/application
     │                            │  {prequal_id}          │
     │                            │───────────────────────►│
     │                            │  {app_id, docs_required}│
     │                            │◄───────────────────────│
     │  Upload docs one by one    │                        │
     │  (gov ID → bank stmts →    │                        │
     │   lease agreement)         │  POST /api/loan/application/{id}/documents
     │                            │───────────────────────►│
     │                            │                        │ state → APP_SUBMITTED
     │                            │  POST /api/loan/application/{id}/route
     │                            │───────────────────────►│
     │                            │                        │ → Kiavi API (TODO stub)
     │                            │                        │ state → UNDERWRITING
     │                            │                        │
     │                   [Kiavi webhook fires 21 days later]
     │                            │  POST /api/loan/webhook/lender-update
     │                            │  {status: "APPROVED"}  │
     │                            │                        │ state → APPROVED
     │                            │                        │ notify via outbound-agent
     │  "Approved!" notification  │                        │
     │◄───────────────────────────│                        │
```

---

## Sequence: PSA Signed → Transaction Coordinator → Closing

```
Investor / Web App          Deal Flow Agents           External Parties
     │                            │                          │
     │  PSA uploaded/executed     │                          │
     │  POST /api/tx/open         │                          │
     │  {psa_terms, closing_date} │                          │
     │──────────────────────────►│                           │
     │                            │ 1. generate_timeline()   │
     │                            │    16 milestones w/ dates │
     │                            │ 2. Insert tx_deadlines   │
     │                            │    inspection: day 10    │
     │                            │    financing:  day 21    │
     │                            │    title:      day 14    │
     │                            │ 3. Seed buyer + seller   │
     │                            │    as tx_parties         │
     │  {tx_id, timeline}         │                          │
     │◄──────────────────────────│                           │
     │                            │                          │
     │  [Day 2] Earnest money sent│                          │
     │  POST /api/tx/{id}/milestone/earnest_money_deposited/complete
     │──────────────────────────►│                           │
     │                            │                          │
     │  POST /api/tx/{id}/party   │                          │
     │  {party_type: "inspector"} │                          │
     │──────────────────────────►│                           │
     │                            │ inspector record created  │
     │                            │                          │
     │  [Day 6] Inspection done   │                          │
     │  POST .../inspection_completed/complete               │
     │──────────────────────────►│                           │
     │                            │                          │
     │  [Day 9] Check deadlines   │                          │
     │  GET /api/tx/{id}/deadlines│                          │
     │──────────────────────────►│                           │
     │                            │ deadline_health_check()  │
     │  {health: "urgent",        │ inspection: 1 day left!  │
     │   urgent: [inspection]}    │                          │
     │◄──────────────────────────│                           │
     │                            │                          │
     │  [Day 10] Waive inspection │                          │
     │  POST .../inspection_response_deadline/complete       │
     │──────────────────────────►│                           │
     │                            │ resolve_deadline()       │
     │                            │ contingency_type=inspection
     │                            │                          │
     │  [Day 21] Financing cleared│                          │
     │  POST .../financing_contingency_deadline/complete     │
     │──────────────────────────►│                           │
     │                            │                          │
     │  [Day 30] CLOSE            │                          │
     │  POST .../closing_day/complete                        │
     │──────────────────────────►│                           │
     │                            │ tx.status = "closed"     │
     │  Deal closed!              │                          │
     │◄──────────────────────────│                           │
```

---

## Database Schema (abbreviated)

```
loan_prequals          loan_applications      loan_documents
  id (PK)               id (PK)                id
  user_id               prequal_id → FK        application_id → FK
  borrower_data (JSON)  user_id                doc_type
  property_data (JSON)  status (enum)          s3_url
  score                 current_state          verified
  suggested_product     lender_partner
  dscr, ltv             docs_required (JSON)  loan_audit_log
  strengths (JSON)      docs_received (JSON)   id
  concerns (JSON)       audit_log (JSON)       application_id → FK
  next_steps (JSON)     approved_amount        event_type
                        approved_rate          payload (JSON)

transactions           tx_parties              tx_milestones
  id (PK)               id                     id
  user_id               transaction_id → FK    transaction_id → FK
  psa_terms (JSON)      party_type             milestone_name
  purchase_price        name, email, phone     sequence_order
  closing_date          company                target_date
  status                                       status
  current_milestone                            completed_at

tx_deadlines           tx_documents            tx_communications
  id                    id                     id
  transaction_id → FK   transaction_id → FK    transaction_id → FK
  contingency_type      doc_type               party_id → FK
  deadline_date         s3_url                 direction (in/out)
  status                status                 channel
  warning_level                                summary
                                               occurred_at
```

---

## Auth Model

```
All endpoints: Authorization: Bearer <TRANCHI_API_SECRET>
               or X-API-Key: <TRANCHI_API_SECRET>

Lender webhooks: X-Webhook-Signature: <HMAC-SHA256 of body>
                 secret stored in LENDER_WEBHOOK_SECRET env var

Same pattern as tranchi-outbound-agent — one config change to wire in.
```
