-- 004_prequal_letters.sql — audit log of every pre-qualification letter sent.
-- We don't persist the PDF bytes (the API returns them inline base64); this
-- table is the compliance trail: who got what numbers, when, what the
-- breakdown was, whether the Zapier hook actually fired.

CREATE TABLE IF NOT EXISTS prequal_letters (
    letter_id           TEXT PRIMARY KEY,           -- pql_<uuid>
    prequal_id          TEXT NOT NULL,              -- FK to loan_prequals.id
    application_id      TEXT DEFAULT '',            -- FK to loan_applications.id when known
    borrower_name       TEXT NOT NULL,
    borrower_email      TEXT DEFAULT '',
    max_pp_low          REAL NOT NULL,
    max_pp_high         REAL NOT NULL,
    liquid_assets       REAL NOT NULL,
    monthly_rent_used   REAL,
    rate_low_pct        REAL NOT NULL,
    rate_high_pct       REAL NOT NULL,
    down_pct_low        REAL NOT NULL,
    breakdown           TEXT DEFAULT '{}',          -- JSON: intake docs used + math
    zap_fired           INTEGER NOT NULL DEFAULT 0, -- 0|1
    sent_to             TEXT DEFAULT '',
    issued_at           TEXT NOT NULL,              -- ISO ts
    expires_at          TEXT NOT NULL,              -- ISO ts (issued_at + 90d)
    created_at          TEXT NOT NULL,
    FOREIGN KEY (prequal_id) REFERENCES loan_prequals(id)
);

CREATE INDEX IF NOT EXISTS idx_prequal_letters_prequal ON prequal_letters (prequal_id);
CREATE INDEX IF NOT EXISTS idx_prequal_letters_app     ON prequal_letters (application_id);
CREATE INDEX IF NOT EXISTS idx_prequal_letters_issued  ON prequal_letters (issued_at);
