"""
loan_officer/typeform/ — Typeform webhook intake for prequal.typeform.com/qualification.

Flow:
    Typeform submission
      → POST /api/loan/webhook/typeform-submit (HMAC verified)
      → map fields → loan_borrower_intakes row
      → soft prequal (credit score gate + doc completeness)
      → AI Loan Officer drafts personalized email
      → send via Zapier MCP gmail action to borrower's email
      → fire borrower_intake_created outbound zap

The form schema is captured in mapper.FIELD_REFS — refs are stable Typeform
identifiers so renaming the question title won't break the mapping.
"""
