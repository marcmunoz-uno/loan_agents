-- 006_tx_coordinator_v2.sql — schema additions for the v2 transaction coordinator.
-- (Renumbered from 005 on merge into loan_agents, which already has a 005_typeform_intake.)
-- Adds:
--   - transactions.psa_execution_date  (Day 0 of the timeline)
--   - transactions.agent_mode          (per-tx shadow/live override)
--   - tx_outbound_messages             (audit log for every escalation/action the agent sends or would send,
--                                       including outbound Arive Zapier actions via channel='arive')
--   - tx_psa_intakes                   (PSA PDF upload → extracted-terms audit trail)
--
-- Inbound Arive status updates ride the existing /api/loan/webhook/arive-update
-- endpoint in loan_agents — there is no separate inbound-event table here.

-- Column additions are handled idempotently in shared.db._apply_schema_patches.
-- Only new tables are declared here; ALTER TABLE on existing tables happens in Python
-- to stay safe across re-runs and to avoid SQLite's lack of IF NOT EXISTS on ALTER.

CREATE TABLE IF NOT EXISTS tx_outbound_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT NOT NULL,
    party_id        INTEGER,                   -- nullable; some messages target the investor
    target_role     TEXT NOT NULL,             -- investor | listing_agent | lender | title | inspector | escrow
    channel         TEXT NOT NULL DEFAULT 'imessage',
    -- channel: imessage | sms | email | voice | webhook
    reason          TEXT NOT NULL,             -- short slug: inspection_deadline_urgent | financing_overdue | etc.
    body            TEXT NOT NULL,             -- the actual message we sent (or would have sent)
    mode            TEXT NOT NULL DEFAULT 'shadow',
    -- mode: shadow (logged, NOT sent) | live (sent through outbound agent)
    outbound_ref    TEXT DEFAULT '',           -- id from tranchi-outbound-agent if mode=live
    sent_at         TEXT NOT NULL,
    error           TEXT DEFAULT '',
    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_tx_outbound_tx_id    ON tx_outbound_messages (transaction_id);
CREATE INDEX IF NOT EXISTS idx_tx_outbound_reason   ON tx_outbound_messages (reason);
CREATE INDEX IF NOT EXISTS idx_tx_outbound_sent_at  ON tx_outbound_messages (sent_at);


CREATE TABLE IF NOT EXISTS tx_psa_intakes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  TEXT,                      -- nullable: set once we open the tx; NULL while extraction is pending
    source_url      TEXT DEFAULT '',           -- s3:// or https:// URL to the original PSA pdf
    extracted_terms TEXT DEFAULT '{}',         -- JSON: PSATerms produced by Claude vision
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    -- status: pending | extracted | failed | accepted
    extraction_error TEXT DEFAULT '',
    extraction_model TEXT DEFAULT '',
    uploaded_at     TEXT NOT NULL,
    accepted_at     TEXT,                      -- set when caller confirmed terms and opened the tx
    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_tx_psa_intakes_tx_id  ON tx_psa_intakes (transaction_id);
CREATE INDEX IF NOT EXISTS idx_tx_psa_intakes_status ON tx_psa_intakes (extraction_status);
