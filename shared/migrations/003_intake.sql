-- 003_intake.sql — Intake document pipeline storage.
-- intake_documents is the staging area: docs that have been presigned, uploaded,
-- classified by OCR, and pending attachment to a loan_application. Once an
-- application_id is set, the row remains as the source of truth for the OCR
-- results; loan_documents holds the verified set the underwriter sees.

CREATE TABLE IF NOT EXISTS intake_documents (
    doc_id              TEXT PRIMARY KEY,           -- "doc_" + uuid hex
    deal_id             TEXT DEFAULT '',            -- intake-time deal context (pre-application)
    application_id      TEXT DEFAULT '',            -- set once the application exists
    user_id             TEXT DEFAULT '',
    filename            TEXT NOT NULL,
    content_type        TEXT NOT NULL,
    size_bytes          INTEGER DEFAULT 0,
    s3_bucket           TEXT NOT NULL,
    s3_key              TEXT NOT NULL,
    declared_doc_type   TEXT DEFAULT '',            -- what the borrower said it was
    classified_doc_type TEXT DEFAULT '',            -- what the OCR classifier returned
    confidence          REAL DEFAULT 0,
    extracted_fields    TEXT DEFAULT '{}',          -- JSON
    warnings            TEXT DEFAULT '[]',          -- JSON array
    -- status lifecycle: presigned -> uploaded -> classifying -> classified | failed
    status              TEXT NOT NULL DEFAULT 'presigned',
    error_message       TEXT DEFAULT '',
    uploaded_at         TEXT DEFAULT '',
    classified_at       TEXT DEFAULT '',
    source              TEXT DEFAULT '',           -- "inbound_email" | "intake_portal" | etc.
    source_message_id   TEXT DEFAULT '',           -- Gmail message-id (or equivalent) for dedup
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intake_docs_deal_id     ON intake_documents (deal_id);
CREATE INDEX IF NOT EXISTS idx_intake_docs_app_id      ON intake_documents (application_id);
CREATE INDEX IF NOT EXISTS idx_intake_docs_user_id     ON intake_documents (user_id);
CREATE INDEX IF NOT EXISTS idx_intake_docs_status      ON intake_documents (status);
