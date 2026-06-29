# Transaction Coordinator

Fills the "Transaction Automation Layer" gap on Tranchi.ai. Opens a deal file when a PSA is executed, generates the milestone timeline, tracks contingency deadlines, and keeps all parties coordinated through to closing.

## Endpoints

All endpoints require `Authorization: Bearer <TRANCHI_API_SECRET>`.

Base URL (local): `http://localhost:5010`

---

### POST /api/tx/open

Open a transaction from PSA terms. Generates the full milestone timeline automatically.

```bash
curl -X POST http://localhost:5010/api/tx/open \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "usr_marc",
    "purchase_price": 95000,
    "earnest_money": 2500,
    "closing_date": "2026-06-13",
    "inspection_period_days": 10,
    "financing_contingency_days": 21,
    "title_contingency_days": 14,
    "buyer_name": "Marc Munoz",
    "buyer_email": "marc@munoz.ltd",
    "buyer_phone": "+13135550100",
    "seller_name": "John Smith",
    "seller_email": "jsmith@example.com",
    "buyer_agent_name": "Sarah Jones",
    "listing_agent_name": "Bob Williams",
    "property_address": "4521 Oak Ln, Detroit MI 48224"
  }'
```

---

### GET /api/tx/:tx_id

Full transaction status — current milestone, days to close, deadline health, party list.

```bash
curl http://localhost:5010/api/tx/tx_abc123 \
  -H "Authorization: Bearer dev-secret-change-me"
```

---

### POST /api/tx/:tx_id/milestone/:milestone_name/complete

Mark a milestone done. Automatically advances the current milestone pointer and resolves contingency deadlines.

```bash
curl -X POST http://localhost:5010/api/tx/tx_abc123/milestone/inspection_completed/complete \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"notes": "Inspector found minor issues — repair request pending."}'
```

Available milestone names: `psa_executed`, `earnest_money_deposited`, `title_ordered`, `inspection_scheduled`, `loan_application_submitted`, `inspection_completed`, `inspection_response_deadline`, `title_commitment_received`, `appraisal_ordered`, `appraisal_completed`, `title_contingency_deadline`, `financing_contingency_deadline`, `clear_to_close`, `closing_disclosure_received`, `final_walkthrough`, `closing_day`

---

### POST /api/tx/:tx_id/party

Add a party (inspector, lender, title company, etc.).

```bash
curl -X POST http://localhost:5010/api/tx/tx_abc123/party \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "party_type": "inspector",
    "name": "Detroit Home Inspectors LLC",
    "email": "schedule@dhiinspect.com",
    "phone": "+13135559999",
    "company": "Detroit Home Inspectors"
  }'
```

---

### GET /api/tx/:tx_id/deadlines

All contingency deadlines with warning levels (overdue, urgent, approaching, none).

```bash
curl http://localhost:5010/api/tx/tx_abc123/deadlines \
  -H "Authorization: Bearer dev-secret-change-me"
```

---

### POST /api/tx/:tx_id/document

Attach a document reference.

```bash
curl -X POST http://localhost:5010/api/tx/tx_abc123/document \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"doc_type": "inspection_report", "s3_url": "s3://tranchi-docs/tx_abc123/inspection.pdf"}'
```

---

### POST /api/tx/:tx_id/communication

Log a communication with a party.

```bash
curl -X POST http://localhost:5010/api/tx/tx_abc123/communication \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{
    "direction": "out",
    "channel": "email",
    "summary": "Sent repair request to listing agent.",
    "full_text": "Hi Bob, attached is our repair request following the 5/10 inspection..."
  }'
```

---

### POST /api/tx/:tx_id/chat

Ask the Tranchi - Transaction Coordinator about deal status.

```bash
curl -X POST http://localhost:5010/api/tx/tx_abc123/chat \
  -H "Authorization: Bearer dev-secret-change-me" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "usr_marc", "message": "What is the next critical deadline I need to worry about?"}'
```

---

## Standard Milestone Timeline

| Milestone | Target Day |
|-----------|------------|
| PSA Executed | Day 0 |
| Earnest Money Deposited | Day 2 |
| Title Ordered | Day 2 |
| Inspection Scheduled | Day 3 |
| Loan Application Submitted | Day 5 |
| Inspection Completed | Day 6 |
| Inspection Response Deadline | Day 10 |
| Title Commitment Received | Day 12 |
| Appraisal Ordered | Day 10 |
| Appraisal Completed | Day 18 |
| Title Contingency Deadline | Day 14 |
| Financing Contingency Removed | Day 21 |
| Clear to Close | Day 25 |
| Closing Disclosure Received | Day 27 |
| Final Walk-Through | Day 28 |
| Closing Day | Day 30 |
