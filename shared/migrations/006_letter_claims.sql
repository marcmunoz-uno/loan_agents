-- 006_letter_claims.sql
-- Cross-process advisory lock for the autonomous prequal-letter trigger.
-- One row per application; `claimed_at` is the unix timestamp of the last
-- letter slot claimed. Combined with BEGIN IMMEDIATE in the claim helper this
-- serializes concurrent classify→letter races across gunicorn workers so a
-- borrower can never receive two letters inside the dedup window.

CREATE TABLE IF NOT EXISTS letter_claims (
    application_id TEXT PRIMARY KEY,
    claimed_at     TEXT NOT NULL
);
