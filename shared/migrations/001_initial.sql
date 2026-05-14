-- 001_initial.sql — Tranchi Deal Flow Agents schema
-- Compatible with SQLite (dev) and MySQL (prod) — no SQLite-specific syntax.
-- Run via: python -c "from shared.db import init_db; init_db()"

-- ── Loan Officer ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS loan_prequals (
    id              TEXT PRIMARY KEY,          -- UUID
    user_id         TEXT NOT NULL,             -- matches Tranchi users.id
    borrower_data   TEXT NOT NULL,             -- JSON: BorrowerProfile
    property_data   TEXT NOT NULL,             -- JSON: PropertyProfile
    score           REAL NOT NULL DEFAULT 0,   -- 0-100 fit score
    suggested_product TEXT NOT NULL DEFAULT '',
    dscr            REAL,
    ltv             REAL,
    monthly_payment_estimate REAL DEFAULT 0,
    strengths       TEXT DEFAULT '[]',         -- JSON array
    concerns        TEXT DEFAULT '[]',         -- JSON array
    next_steps      TEXT DEFAULT '[]',         -- JSON array
    status          TEXT NOT NULL DEFAULT 'scored',
    notes           TEXT DEFAULT '',
    created_at      TEXT NOT NULL,             -- ISO datetime
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_loan_prequals_user_id ON loan_prequals (user_id);
CREATE INDEX IF NOT EXISTS idx_loan_prequals_status  ON loan_prequals (status);


CREATE TABLE IF NOT EXISTS loan_applications (
    id              TEXT PRIMARY KEY,          -- UUID
    prequal_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'APP_STARTED',
    -- status enum: NEW | PREQUAL_PENDING | PREQUAL_SCORED | APP_STARTED |
    --              APP_DOCS_PENDING | APP_SUBMITTED | UNDERWRITING |
    --              APPROVED | DECLINED | CONDITIONS | CLOSING | FUNDED
    current_state   TEXT NOT NULL DEFAULT 'APP_STARTED',
    lender_partner  TEXT DEFAULT '',
    lender_ref_id   TEXT DEFAULT '',           -- lender's application ID
    docs_required   TEXT DEFAULT '[]',         -- JSON array of doc_type strings
    docs_received   TEXT DEFAULT '[]',         -- JSON array of doc_type strings
    underwriter_notes TEXT DEFAULT '',
    approved_amount REAL,
    approved_rate   REAL,
    approved_term   INTEGER,
    conditions      TEXT DEFAULT '[]',         -- JSON: conditions to close
    audit_log       TEXT DEFAULT '[]',         -- JSON array of events
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (prequal_id) REFERENCES loan_prequals(id)
);

CREATE INDEX IF NOT EXISTS idx_loan_apps_prequal_id ON loan_applications (prequal_id);
CREATE INDEX IF NOT EXISTS idx_loan_apps_user_id    ON loan_applications (user_id);
CREATE INDEX IF NOT EXISTS idx_loan_apps_status     ON loan_applications (status);


CREATE TABLE IF NOT EXISTS loan_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    -- doc_type examples: pay_stub, bank_statement, tax_return, lease_agreement,
    --   photo_id, entity_docs, appraisal, title_commitment, purchase_contract
    s3_url          TEXT NOT NULL,
    verified        INTEGER NOT NULL DEFAULT 0,  -- 0=no, 1=yes
    uploaded_by     TEXT DEFAULT '',
    notes           TEXT DEFAULT '',
    uploaded_at     TEXT NOT NULL,
    FOREIGN KEY (application_id) REFERENCES loan_applications(id)
);

CREATE INDEX IF NOT EXISTS idx_loan_docs_app_id ON loan_documents (application_id);


CREATE TABLE IF NOT EXISTS loan_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    -- event_type examples: state_transition, doc_uploaded, lender_routed,
    --   lender_status_update, manual_note, approval, decline
    payload         TEXT DEFAULT '{}',          -- JSON
    actor           TEXT DEFAULT 'system',
    created_at      TEXT NOT NULL,
    FOREIGN KEY (application_id) REFERENCES loan_applications(id)
);

CREATE INDEX IF NOT EXISTS idx_loan_audit_app_id ON loan_audit_log (application_id);


-- ── Transaction Coordinator ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS transactions (
    id              TEXT PRIMARY KEY,           -- UUID
    user_id         TEXT NOT NULL,
    psa_terms       TEXT NOT NULL,              -- JSON: PSATerms
    purchase_price  REAL NOT NULL,
    closing_date    TEXT NOT NULL,              -- ISO date
    status          TEXT NOT NULL DEFAULT 'open',
    -- status: open | closing | closed | cancelled | fallen_through
    current_milestone TEXT DEFAULT 'psa_executed',
    property_address TEXT DEFAULT '',
    buyer_name      TEXT DEFAULT '',
    seller_name     TEXT DEFAULT '',
    notes           TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_user_id  ON transactions (user_id);
CREATE INDEX IF NOT EXISTS idx_tx_status   ON transactions (status);


CREATE TABLE IF NOT EXISTS tx_parties (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT NOT NULL,
    party_type      TEXT NOT NULL,
    -- party_type: buyer | seller | buyer_agent | listing_agent |
    --   title | escrow | inspector | lender | insurance | other
    name            TEXT NOT NULL,
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    company         TEXT DEFAULT '',
    contact_data    TEXT DEFAULT '{}',          -- JSON: extra contact details
    added_at        TEXT NOT NULL,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_tx_parties_tx_id ON tx_parties (transaction_id);


CREATE TABLE IF NOT EXISTS tx_milestones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT NOT NULL,
    milestone_name  TEXT NOT NULL,
    milestone_label TEXT DEFAULT '',            -- human-readable
    sequence_order  INTEGER NOT NULL DEFAULT 0,
    target_date     TEXT NOT NULL,              -- ISO date
    completed_at    TEXT,                       -- ISO datetime when done
    status          TEXT NOT NULL DEFAULT 'pending',
    -- status: pending | in_progress | completed | overdue | waived
    notes           TEXT DEFAULT '',
    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_tx_milestones_tx_id ON tx_milestones (transaction_id);
CREATE INDEX IF NOT EXISTS idx_tx_milestones_status ON tx_milestones (status);


CREATE TABLE IF NOT EXISTS tx_deadlines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT NOT NULL,
    contingency_type TEXT NOT NULL,
    -- contingency_type: inspection | financing | title | appraisal | hoa | other
    deadline_date   TEXT NOT NULL,              -- ISO date
    resolved_at     TEXT,                       -- ISO datetime when resolved/waived
    status          TEXT NOT NULL DEFAULT 'active',
    -- status: active | resolved | waived | expired
    warning_level   TEXT DEFAULT 'none',
    -- warning_level: none | approaching (3+ days) | urgent (1-2 days) | overdue
    notes           TEXT DEFAULT '',
    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_tx_deadlines_tx_id ON tx_deadlines (transaction_id);
CREATE INDEX IF NOT EXISTS idx_tx_deadlines_date  ON tx_deadlines (deadline_date);


CREATE TABLE IF NOT EXISTS tx_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT NOT NULL,
    doc_type        TEXT NOT NULL,
    -- doc_type examples: psa, addendum, disclosure, inspection_report,
    --   title_commitment, appraisal, loan_approval, closing_disclosure,
    --   hud1, deed, wire_instructions, survey, hoa_docs
    s3_url          TEXT NOT NULL,
    party_uploaded  TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'received',
    -- status: received | reviewed | approved | rejected | superseded
    notes           TEXT DEFAULT '',
    uploaded_at     TEXT NOT NULL,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_tx_docs_tx_id ON tx_documents (transaction_id);


CREATE TABLE IF NOT EXISTS tx_communications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT NOT NULL,
    party_id        INTEGER,                    -- FK to tx_parties; NULL = general
    direction       TEXT NOT NULL DEFAULT 'out',-- in | out
    channel         TEXT NOT NULL DEFAULT 'email',
    -- channel: email | sms | imessage | call | in_person | portal
    summary         TEXT NOT NULL,
    full_text       TEXT DEFAULT '',
    occurred_at     TEXT NOT NULL,              -- ISO datetime
    logged_at       TEXT NOT NULL,              -- when we recorded it
    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
    FOREIGN KEY (party_id) REFERENCES tx_parties(id)
);

CREATE INDEX IF NOT EXISTS idx_tx_comms_tx_id ON tx_communications (transaction_id);
CREATE INDEX IF NOT EXISTS idx_tx_comms_party ON tx_communications (party_id);
