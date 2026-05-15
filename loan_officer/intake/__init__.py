"""
loan_officer/intake/ — Document intake sub-package for the Tranchi - Loan Officer persona.

Handles the full document collection pipeline:
  chatbot        → conversational intake session with deal-context tracking
  doc_upload     → multipart upload handling, virus scan stub, S3/R2 push
  ocr_classifier → Claude vision-based doc type detection + field extraction
  completeness   → per-loan-type doc checklist + missing-doc reporter
  routes         → Flask blueprint: /api/intake/*
"""
