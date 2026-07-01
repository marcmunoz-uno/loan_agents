-- 007_tx_guardrails.sql — key/value store for transaction-coordinator runtime
-- controls. Today it holds the live-mode kill switch; the sweeper reads it on
-- every tick so "stop all live sending" takes effect immediately with no
-- redeploy. Generic on purpose so future runtime toggles don't need a migration.

CREATE TABLE IF NOT EXISTS tx_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
