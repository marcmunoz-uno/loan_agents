"""
loan_officer/typeform/tests/test_letter_autofire.py — Autofire pipeline tests.

Run: python -m pytest loan_officer/typeform/tests/test_letter_autofire.py -v

These mock the network boundary (Typeform CDN download + Claude vision +
Zapier MCP send) so the suite is hermetic. The integration with the live
Zapier endpoint is exercised by the `tranchi-prequal-letter` skill.
"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_db(monkeypatch):
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    monkeypatch.setenv("DB_PATH", tmp_db.name)
    monkeypatch.delenv("ZAPIER_MCP_ENDPOINT", raising=False)
    monkeypatch.delenv("ZAPIER_MCP_API_KEY", raising=False)

    import importlib
    import shared.db
    importlib.reload(shared.db)
    shared.db.init_db()
    yield tmp_db.name
    os.unlink(tmp_db.name)


def _intake_row(**overrides):
    base = {
        "intake_id": "bi_test123456",
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
        "phone": "+15555550101",
        "company": "Doe Investments LLC",
        "credit_score_estimate": 740,
        "soft_prequal_score": 80,
        "asset_statement_recent_url":   "https://api.typeform.com/uploaded/recent.pdf",
        "asset_statement_previous_url": "https://api.typeform.com/uploaded/previous.pdf",
        "asset_statement_extra_url":    "",
    }
    base.update(overrides)
    return base


def _seed_intake(db_path, row):
    """Insert the intake row so the autofire can UPDATE it later."""
    from shared.db import get_conn, insert
    with get_conn() as conn:
        insert(conn, "loan_borrower_intakes", {
            **row,
            "typeform_response_id": f"tf_{row['intake_id']}",
            "typeform_form_id":      "qualification",
            "submitted_at":          "2026-05-25T00:00:00Z",
            "received_at":           "2026-05-25T00:00:00Z",
            "soft_prequal_status":   "pass",
            "missing_required_docs": "[]",
            "decision_reasons":      "[]",
            "email_send_status":     "letter_pending",
            "raw_payload":           "{}",
        })


# ── Parser ───────────────────────────────────────────────────────────────────

class TestParseBalance:
    def test_parses_clean_json(self):
        from loan_officer.typeform.letter_autofire import _parse_balance_response
        out = _parse_balance_response('{"ending_balance": 42500.55, "bank_name": "Chase", "statement_period": "2026-04"}')
        assert out["ending_balance"] == 42500.55
        assert out["bank_name"] == "Chase"

    def test_handles_fenced_json(self):
        from loan_officer.typeform.letter_autofire import _parse_balance_response
        out = _parse_balance_response("```json\n{\"ending_balance\": 12345}\n```")
        assert out["ending_balance"] == 12345.0

    def test_handles_string_with_dollar_sign(self):
        from loan_officer.typeform.letter_autofire import _parse_balance_response
        out = _parse_balance_response('{"ending_balance": "$24,800.00"}')
        assert out["ending_balance"] == 24800.00

    def test_returns_none_for_garbage(self):
        from loan_officer.typeform.letter_autofire import _parse_balance_response
        out = _parse_balance_response("hello world")
        assert out["ending_balance"] is None

    def test_falls_back_to_alternate_balance_field(self):
        from loan_officer.typeform.letter_autofire import _parse_balance_response
        out = _parse_balance_response('{"available_balance": 9999}')
        assert out["ending_balance"] == 9999.0


# ── End-to-end pipeline (mocked) ─────────────────────────────────────────────

class TestAutofirePipeline:
    def test_skips_when_no_asset_statements(self, isolated_db):
        from loan_officer.typeform.letter_autofire import _fire_letter_sync
        row = _intake_row(asset_statement_recent_url="", asset_statement_previous_url="")
        _seed_intake(isolated_db, row)
        result = _fire_letter_sync(row["intake_id"], row)
        assert result["skipped"] == "no_asset_statements"

    def test_skips_when_no_email(self, isolated_db):
        from loan_officer.typeform.letter_autofire import _fire_letter_sync
        row = _intake_row(email="")
        _seed_intake(isolated_db, row)
        result = _fire_letter_sync(row["intake_id"], row)
        assert result["skipped"] == "no_borrower_email"

    def test_skips_when_ocr_yields_below_threshold(self, isolated_db, monkeypatch):
        from loan_officer.typeform import letter_autofire
        row = _intake_row()
        _seed_intake(isolated_db, row)
        # Mock the network: download succeeds, OCR returns trivial balance.
        monkeypatch.setattr(letter_autofire, "_download",
                            lambda url: (b"%PDF-mock", "application/pdf"))
        monkeypatch.setattr(letter_autofire, "chat_with_vision",
                            lambda **kw: '{"ending_balance": 100}')
        result = letter_autofire._fire_letter_sync(row["intake_id"], row)
        assert result["skipped"] == "liquid_assets_below_threshold"
        assert result["liquid_assets"] == 200.0   # 2 docs × $100

    def test_fires_letter_when_liquidity_meets_threshold(self, isolated_db, monkeypatch):
        from loan_officer.typeform import letter_autofire
        row = _intake_row()
        _seed_intake(isolated_db, row)

        monkeypatch.setattr(letter_autofire, "_download",
                            lambda url: (b"%PDF-mock", "application/pdf"))
        monkeypatch.setattr(letter_autofire, "chat_with_vision",
                            lambda **kw: '{"ending_balance": 60000}')

        # Mock generate_and_send so we don't actually render reportlab + send.
        from loan_officer import prequal_letter as pl
        captured: dict = {}

        class _FakeLetter:
            letter_id = "pql_fakeabc12345"
            max_pp_low = 285_000.0
            max_pp_high = 320_000.0
            mcp_send_status = "sent"
            zap_fired = True
            issued_at = "2026-05-25T00:00:00+00:00"

        def _fake_generate_and_send(prequal_id, **kw):
            captured["prequal_id"] = prequal_id
            captured["kw"] = kw
            return _FakeLetter()

        monkeypatch.setattr(pl, "generate_and_send", _fake_generate_and_send)

        result = letter_autofire._fire_letter_sync(row["intake_id"], row)

        assert result["ok"] is True
        assert result["letter_id"] == "pql_fakeabc12345"
        assert result["liquid_assets"] == 120_000.0    # 2 docs × $60K
        assert captured["kw"]["liquid_assets_override"] == 120_000.0
        assert captured["prequal_id"].startswith("pq_")

        # Intake row should now be marked letter_sent.
        from shared.db import get_conn, fetchone
        with get_conn() as conn:
            updated = fetchone(
                conn,
                "SELECT email_send_status, letter_id, liquid_assets_computed "
                "FROM loan_borrower_intakes WHERE intake_id = ?",
                (row["intake_id"],),
            )
        assert updated["email_send_status"] == "letter_sent"
        assert updated["letter_id"] == "pql_fakeabc12345"
        assert updated["liquid_assets_computed"] == 120_000.0
