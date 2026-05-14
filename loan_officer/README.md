# AI Loan Officer

Fills the "Automated Funding System" gap on Tranchi.ai. Guides investors through pre-qualification, routes them to the right loan product, collects required documents, and submits to lender partners.

## Endpoints

All endpoints require `Authorization: Bearer <TRANCHI_API_SECRET>`.

Base URL (local): `http://localhost:5010`

---

### POST /api/loan/prequal

Pre-qualify a borrower. Scores the deal, suggests the best product, estimates monthly payment.

```bash
curl -X POST http://localhost:5010/api/loan/prequal \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "borrower": {
      "user_id": "usr_marc",
      "name": "Marc Munoz",
      "credit_score": 740,
      "annual_income": 120000,
      "liquidity": 85000,
      "properties_owned": 3,
      "loan_purpose": "purchase",
      "desired_loan_amount": 72000,
      "down_payment_pct": 25
    },
    "property": {
      "address": "4521 Oak Ln, Detroit MI 48224",
      "property_type": "single_family",
      "purchase_price": 95000,
      "estimated_value": 110000,
      "monthly_rent": 1200,
      "annual_taxes": 2400,
      "annual_insurance": 1200
    }
  }'
```

---

### GET /api/loan/prequal/:prequal_id

Fetch a saved prequal result.

```bash
curl http://localhost:5010/api/loan/prequal/pq_abc123 \
  -H "Authorization: Bearer dev-secret-change-me"
```

---

### POST /api/loan/application

Open a formal application from a prequal.

```bash
curl -X POST http://localhost:5010/api/loan/application \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "prequal_id": "pq_abc123",
    "borrower": { "user_id": "usr_marc", "name": "Marc Munoz" },
    "property": { "address": "4521 Oak Ln Detroit MI", "property_type": "single_family" }
  }'
```

---

### POST /api/loan/application/:id/documents

Upload document references (S3 URLs).

```bash
curl -X POST http://localhost:5010/api/loan/application/app_abc123/documents \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "documents": [
      {"doc_type": "government_id", "s3_url": "s3://tranchi-docs/marc/id.jpg"},
      {"doc_type": "bank_statement_3mo", "s3_url": "s3://tranchi-docs/marc/bank.pdf"}
    ]
  }'
```

---

### GET /api/loan/application/:id

Fetch full application status, missing docs, audit log.

```bash
curl http://localhost:5010/api/loan/application/app_abc123 \
  -H "Authorization: Bearer dev-secret-change-me"
```

---

### POST /api/loan/application/:id/route

Submit application to a lender partner.

```bash
curl -X POST http://localhost:5010/api/loan/application/app_abc123/route \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"lender_slug": "kiavi"}'
```

---

### POST /api/loan/webhook/lender-update

Lender pushes status back (approval/decline/conditions). Signed with `LENDER_WEBHOOK_SECRET`.

```bash
curl -X POST http://localhost:5010/api/loan/webhook/lender-update \
  -H "Content-Type: application/json" \
  -d '{
    "lender_ref_id": "STUB-KIAVI-ABC123",
    "status": "APPROVED",
    "approved_amount": 72000,
    "approved_rate": 7.75,
    "approved_term": 360,
    "notes": "Approved. Strong DSCR."
  }'
```

---

### POST /api/loan/chat

Conversational AI loan officer (Alex).

```bash
curl -X POST http://localhost:5010/api/loan/chat \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "usr_marc", "message": "What DSCR do I need for a buy-and-hold in Detroit?"}'
```

---

## State Machine

```
NEW → PREQUAL_PENDING → PREQUAL_SCORED → APP_STARTED → APP_DOCS_PENDING
    → APP_SUBMITTED → UNDERWRITING → APPROVED | CONDITIONS | DECLINED
    → CLOSING → FUNDED
```

## Lender Partners

- Lima One Capital (DSCR, Fix & Flip)
- Kiavi (DSCR, BRRRR, Fix & Flip)
- New Silver (Fix & Flip)
- LendingOne (DSCR)
- Roc Capital (DSCR, BRRRR, Fix & Flip)
- Anchor Loans (Fix & Flip, select states)

All lender API integrations are stubbed. Replace `# TODO: integrate with lender X` comments in `routes.py` with real API calls when lender partnerships are established.
