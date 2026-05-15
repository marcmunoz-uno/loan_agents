# Tranchi - Loan Processor

The Loan Processor pre-underwrites every loan file before it hits a lender's desk. It reads lender guidelines, scores the deal, generates conditions, flags red flags, and drafts the credit memo.

## Endpoints

All endpoints require `Authorization: Bearer <TRANCHI_API_SECRET>`.

---

### POST /api/processor/pre-underwrite/:application_id

Run pre-underwriting on an application. Returns a full `PreUnderwritingReport`.

If `overall_status` is `clean`, automatically fires `ready_for_underwriting` Zapier event to push the file into Arive with the credit memo attached.

```bash
curl -X POST http://localhost:5010/api/processor/pre-underwrite/app_seed_marc_001 \
  -H "Authorization: Bearer dev-secret-change-me"
```

---

### GET /api/processor/pre-underwrite/:application_id

Fetch the latest pre-underwriting report.

```bash
curl http://localhost:5010/api/processor/pre-underwrite/app_seed_marc_001 \
  -H "Authorization: Bearer dev-secret-change-me"
```

---

### GET /api/processor/guidelines

Return the full `guidelines_index.json` — lender matrix for all products.

```bash
curl http://localhost:5010/api/processor/guidelines \
  -H "Authorization: Bearer dev-secret-change-me"
```

---

### GET /api/processor/guidelines/:lender_id

Return the full markdown guidelines for one lender. `lender_id` values are the keys in `guidelines_index.json`.

```bash
curl http://localhost:5010/api/processor/guidelines/lima_one_dscr \
  -H "Authorization: Bearer dev-secret-change-me"
```

---

### POST /api/processor/guidelines/check

Quick lender match without running full pre-UW. Pass borrower + property data, get back ranked lenders.

```bash
curl -X POST http://localhost:5010/api/processor/guidelines/check \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "borrower_data": {"credit_score": 720, "down_payment_pct": 25, "loan_purpose": "purchase"},
    "property_data": {
      "address": "4521 Oak Ln Detroit MI 48224",
      "property_type": "single_family",
      "purchase_price": 95000,
      "monthly_rent": 1200,
      "annual_taxes": 2400,
      "annual_insurance": 1200
    }
  }'
```

---

### POST /api/processor/chat

Conversational interface for the Tranchi - Loan Processor. Ask a guideline question.

```bash
curl -X POST http://localhost:5010/api/processor/chat \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "usr_marc", "message": "What is Lima One'\''s minimum DSCR for a purchase?", "lender_id": "lima_one_dscr"}'
```

---

### POST /api/processor/fire-zap/ready-for-underwriting

Manually fire the Zapier webhook to push a file into Arive as "Ready for Submission."

```bash
curl -X POST http://localhost:5010/api/processor/fire-zap/ready-for-underwriting \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"application_id": "app_seed_marc_001"}'
```

---

## Pre-Underwriting Report Schema

```json
{
  "application_id": "app_abc123",
  "summary": "Clean file for Lima One DSCR — 2 PTSU conditions",
  "overall_status": "clean | conditional | decline_risk",
  "lender_fit": [
    {
      "lender_id": "lima_one_dscr",
      "lender": "Lima One Capital",
      "fit_score": 92,
      "qualifies": true,
      "qualify_reasons": ["FICO 720 meets min 660", "LTV 75% within 80% max"],
      "decline_reasons": [],
      "hot_buttons": ["DSCR calculated on gross rent only"]
    }
  ],
  "conditions": [
    {
      "condition_type": "doc_request",
      "severity": "prior_to_submission",
      "description": "Tri-merge credit report...",
      "lender_specific": "lima_one_dscr",
      "required": true
    }
  ],
  "red_flags": [
    {
      "flag_type": "dscr_too_low",
      "severity": "minor",
      "description": "DSCR 1.08 is close to the floor...",
      "mitigation_suggestion": "..."
    }
  ],
  "computed_metrics": {
    "fico": 720,
    "ltv": 0.75,
    "dscr": 1.18,
    "monthly_piti": 750.00,
    "monthly_cashflow": 450.00
  },
  "suggested_lender": "Lima One Capital",
  "credit_memo_draft": "## Borrower\n...",
  "generated_at": "2026-05-15T00:00:00Z"
}
```

## Loan Officer → Loan Processor → Arive Flow

```
Tranchi - Loan Officer
  POST /api/loan/application → application created
  POST /api/loan/application/:id/documents → all docs uploaded
        │
        ▼
Tranchi - Loan Processor
  POST /api/processor/pre-underwrite/:id → runs pre-UW
    if clean → automatically fires Zapier → Arive "Ready for Submission"
    if conditional → fires Zapier note with conditions, does not advance status
        │
        ▼
Arive LOS (via Zapier)
  Record created/updated in Arive
  Underwriter reviews and issues decision
  Arive fires webhook back → POST /api/loan/webhook/arive-update
        │
        ▼
Tranchi - Loan Officer
  Status updated in our DB
  Application advances to UNDERWRITING → APPROVED / CONDITIONS / DECLINED
```
