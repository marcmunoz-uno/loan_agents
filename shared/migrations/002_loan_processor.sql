-- 002_loan_processor.sql — Loan Processor (Casey) tables
-- Run automatically via init_db() on first startup.
-- Compatible with SQLite (dev) and MySQL (prod).

-- ── Pre-Underwriting Reports ──────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS pre_underwriting_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    -- status: pending | clean | conditional | decline_risk
    summary         TEXT DEFAULT '',                -- one-liner
    overall_status  TEXT NOT NULL DEFAULT 'pending',-- clean | conditional | decline_risk
    lender_fit      TEXT DEFAULT '[]',              -- JSON: ranked list of lender matches
    conditions      TEXT DEFAULT '[]',              -- JSON: list of Condition objects
    red_flags       TEXT DEFAULT '[]',              -- JSON: list of RedFlag objects
    computed_metrics TEXT DEFAULT '{}',             -- JSON: DSCR, LTV, LTC, monthly metrics
    suggested_lender TEXT DEFAULT '',               -- e.g. "Lima One Capital"
    credit_memo     TEXT DEFAULT '',                -- LLM-drafted ~300 word narrative
    generated_at    TEXT NOT NULL,                  -- ISO datetime
    FOREIGN KEY (application_id) REFERENCES loan_applications(id)
);

CREATE INDEX IF NOT EXISTS idx_preuw_app_id ON pre_underwriting_reports (application_id);
CREATE INDEX IF NOT EXISTS idx_preuw_status ON pre_underwriting_reports (overall_status);
CREATE INDEX IF NOT EXISTS idx_preuw_generated ON pre_underwriting_reports (generated_at);


-- ── Lender Guidelines Cache ───────────────────────────────────────────────────
-- Tracks when lender guidelines were last synced.
-- The source of truth is lender_guidelines/*.md — this table is for change detection.

CREATE TABLE IF NOT EXISTS lender_guidelines_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lender_id       TEXT NOT NULL UNIQUE,           -- e.g. "lima_one_dscr"
    last_synced_at  TEXT NOT NULL,                  -- ISO datetime
    source_url      TEXT DEFAULT '',                -- lender's published guidelines URL
    content_hash    TEXT DEFAULT '',                -- SHA256 of guideline doc for change detection
    notes           TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_guideline_lender ON lender_guidelines_cache (lender_id);
