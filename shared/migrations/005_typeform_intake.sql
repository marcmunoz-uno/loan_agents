-- 003_typeform_intake.sql — Typeform borrower-intake table
-- Run automatically via init_db() on first startup.
-- Compatible with SQLite (dev) and MySQL (prod).

CREATE TABLE IF NOT EXISTS loan_borrower_intakes (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id                   TEXT NOT NULL UNIQUE,           -- bi_<hex12>
    typeform_response_id        TEXT NOT NULL UNIQUE,           -- dedupes Typeform retries
    typeform_form_id            TEXT NOT NULL,
    submitted_at                TEXT NOT NULL,                  -- ISO8601 from Typeform
    received_at                 TEXT NOT NULL,                  -- ISO8601 server clock

    -- Borrower contact
    first_name                  TEXT DEFAULT '',
    last_name                   TEXT DEFAULT '',
    email                       TEXT DEFAULT '',
    phone                       TEXT DEFAULT '',
    company                     TEXT DEFAULT '',

    -- Profile
    dob                         TEXT DEFAULT '',                -- raw string from form
    married                     INTEGER,                        -- 1/0/NULL
    credit_score_estimate       INTEGER,                        -- parsed from short_text
    primary_residence_status    TEXT DEFAULT '',                -- own | rent | living_rent_free
    primary_residence_years     REAL,
    spoke_to_loan_officer       TEXT DEFAULT '',                -- yazan | austin | jeff | joseph | none

    -- File uploads (Typeform-hosted URLs)
    drivers_license_front_url   TEXT DEFAULT '',
    drivers_license_back_url    TEXT DEFAULT '',
    proof_of_residence_url      TEXT DEFAULT '',
    articles_of_org_url         TEXT DEFAULT '',
    operating_agreement_url     TEXT DEFAULT '',
    ein_document_url            TEXT DEFAULT '',
    asset_statement_recent_url  TEXT DEFAULT '',
    asset_statement_previous_url TEXT DEFAULT '',
    asset_statement_extra_url   TEXT DEFAULT '',

    -- Authorization
    credit_pull_authorized      INTEGER NOT NULL DEFAULT 0,     -- 1 if checkbox ticked

    -- Soft prequal result
    soft_prequal_status         TEXT NOT NULL DEFAULT 'pending',-- pass | conditional | decline | pending
    soft_prequal_score          INTEGER,                        -- 0-100
    missing_required_docs       TEXT DEFAULT '[]',              -- JSON array of doc labels
    decision_reasons            TEXT DEFAULT '[]',              -- JSON array of strings

    -- AI Loan Officer email send
    email_send_status           TEXT NOT NULL DEFAULT 'pending',-- sent | failed | skipped | pending | letter_pending | letter_sent | letter_failed | letter_skipped
    email_subject               TEXT DEFAULT '',
    email_body                  TEXT DEFAULT '',
    email_sent_at               TEXT DEFAULT '',
    email_error                 TEXT DEFAULT '',

    -- Prequal-letter autofire link (loan_officer/typeform/letter_autofire.py)
    letter_id                   TEXT DEFAULT '',                -- FK to prequal_letters.letter_id
    liquid_assets_computed      REAL,                           -- OCR'd sum from asset_statement_*_url

    -- Raw payload for auditing / replay
    raw_payload                 TEXT NOT NULL DEFAULT '{}',     -- full Typeform JSON

    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_intakes_email ON loan_borrower_intakes(email);
CREATE INDEX IF NOT EXISTS idx_intakes_submitted_at ON loan_borrower_intakes(submitted_at);
