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

Conversational interface for the Tranchi - Loan Officer.

```bash
curl -X POST http://localhost:5010/api/loan/chat \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "usr_marc", "message": "What DSCR do I need for a buy-and-hold in Detroit?"}'
```

---

## Arive Integration via Zapier

Arive is Tranchi's LOS (Loan Origination System). Arive has no public API — all automation goes through Zapier webhooks.

### Two-way integration pattern

```
Outbound (us → Zapier → Arive):
  Our event fires → POST to Zapier webhook URL → Zapier action: Create/Update in Arive

Inbound (Arive → Zapier → us):
  Arive status changes → Zapier trigger: Arive status change → POST to /api/loan/webhook/arive-update
```

### Outbound Zaps to create (one per event type)

| Event | Zapier Trigger | Arive Action | Env Var |
|---|---|---|---|
| `prequal_created` | Webhooks by Zapier — Catch Hook | Create Lead in Arive | `ZAPIER_HOOK_PREQUAL_CREATED` |
| `application_submitted` | Webhooks by Zapier — Catch Hook | Create Application in Arive | `ZAPIER_HOOK_APPLICATION_SUBMITTED` |
| `documents_uploaded` | Webhooks by Zapier — Catch Hook | Update Application — mark docs received | `ZAPIER_HOOK_DOCUMENTS_UPLOADED` |
| `ready_for_underwriting` | Webhooks by Zapier — Catch Hook | Update Application status → "Ready for Submission" + attach credit memo | `ZAPIER_HOOK_READY_FOR_UNDERWRITING` |
| `lender_routed` | Webhooks by Zapier — Catch Hook | Update Application — set lender partner | `ZAPIER_HOOK_LENDER_ROUTED` |

For each Zap:
1. In Zapier, create a new Zap
2. Trigger: "Webhooks by Zapier" → "Catch Hook" → copy the webhook URL
3. Paste the URL in `.env` as the appropriate `ZAPIER_HOOK_*` variable
4. Action: Connect your Arive account → select "Create Record" or "Update Record"
5. Map fields: `loan_amount`, `borrower_first_name`, `borrower_last_name`, `borrower_email`, etc. (all present in payload)
6. Test + turn on the Zap

### Inbound Zap (Arive → us)

1. Create a new Zap
2. Trigger: "Arive" → "New Status Change" (or whatever Arive exposes as webhook trigger)
3. Action: "Webhooks by Zapier" → "POST" → URL: `https://your-deploy-url.com/api/loan/webhook/arive-update`
4. Set headers: `Content-Type: application/json`
5. Map body fields:
   - `correlation_id` → Arive application's external reference / our app ID
   - `status` → Arive's status string (e.g. "Cleared to Close")
   - `conditions` → (optional) list of UW conditions
   - `notes` → underwriter notes

### HMAC signature

Set `ARIVE_WEBHOOK_SECRET` in `.env` and configure the same value in Arive's webhook settings. All inbound webhooks must include `X-Arive-Signature: <HMAC-SHA256-hex>` in the header. The HMAC is computed over the raw request body using the shared secret.

If `ARIVE_WEBHOOK_SECRET` is empty (dev mode), signature verification is skipped.

### Arive status vocabulary mapping

| Arive Status | Our Internal State |
|---|---|
| Initial Disclosures Sent | APP_SUBMITTED |
| Submitted to Underwriting | UNDERWRITING |
| Conditional Approval | CONDITIONS |
| Cleared to Close | CLOSING |
| Funded | FUNDED |
| Approved | APPROVED |
| Declined | DECLINED |

### Field payload format

Every outbound webhook carries these fields:

```json
{
  "loan_amount": 71250,
  "purchase_price": 95000,
  "down_payment": 23750,
  "down_payment_pct": 25.0,
  "loan_purpose": "purchase",
  "loan_product": "DSCR",
  "property_address": "4521 Oak Ln",
  "property_city": "Detroit",
  "property_state": "MI",
  "property_zip": "48224",
  "subject_property_type": "single_family",
  "borrower_first_name": "Marc",
  "borrower_last_name": "Munoz",
  "borrower_email": "marc@munoz.ltd",
  "borrower_phone": "+13135550100",
  "borrower_fico_estimate": 740,
  "estimated_monthly_rent": 1200,
  "estimated_dscr": 1.18,
  "mlo_assignment": "round_robin",
  "intake_source": "tranchi.ai",
  "correlation_id": "app_abc123",
  "event_type": "prequal_created",
  "sent_at": "2026-05-15T12:00:00Z",
  "source": "tranchi.ai"
}
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
