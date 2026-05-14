# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Tranchi Web App                                  │
│              (Express + tRPC + React, MySQL, 37 tables)             │
│   users | investor_profiles | placed_offers | cash_buyers | deals   │
└──────────────┬──────────────────────────────────────────────────────┘
               │ POST /api/loan/*  POST /api/tx/*
               │ Authorization: Bearer TRANCHI_API_SECRET
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│             tranchi-deal-flow-agents (this repo)                     │
│                        Flask · :5010                                 │
│  ┌──────────────────────────┐  ┌──────────────────────────────────┐ │
│  │    AI Loan Officer       │  │    Transaction Coordinator       │ │
│  │    /api/loan/*           │  │    /api/tx/*                     │ │
│  │                          │  │                                  │ │
│  │  prequal → routing →     │  │  PSA → timeline → milestones →   │ │
│  │  application → docs →    │  │  deadlines → parties → comms →  │ │
│  │  underwriting → funded   │  │  closing day                     │ │
│  └──────────┬───────────────┘  └──────────────┬───────────────────┘ │
│             │                                  │                     │
│  ┌──────────▼──────────────────────────────────▼───────────────────┐│
│  │                      shared/                                     ││
│  │  db.py · llm.py · auth.py · webhooks.py · schemas.py            ││
│  │  tranchi_client.py · migrations/001_initial.sql                  ││
│  └─────────────────────────────────────────────────────────────────┘│
└──────┬───────────────────────────────────────────┬──────────────────┘
       │                                           │
       │ Lender Partner APIs                       │ tranchi-outbound-agent
       │ (HTTP POST application)                   │ (iMessage / voice)
       ▼                                           ▼
┌──────────────────┐   ┌────────────┐   ┌────────────────────────────┐
│  Lima One Capital│   │   Kiavi    │   │  tranchi-outbound-agent     │
│  Roc Capital     │   │ New Silver │   │  POST /api/outreach/nurture │
│  LendingOne      │   │ Anchor     │   │  POST /api/outreach/call    │
└──────────────────┘   └────────────┘   └────────────────────────────┘
                                                    │
                                          ┌─────────▼──────────┐
                                          │ ElevenLabs / Blooio│
                                          │ (voice + iMessage) │
                                          └────────────────────┘

County Portal Scraper ──────────────────────────────────────────────►
  tranchi_mcp_server.py (9 tools)   →  property data feeds into prequal
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
