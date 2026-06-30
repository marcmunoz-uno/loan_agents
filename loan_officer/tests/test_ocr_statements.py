"""
loan_officer/tests/test_ocr_statements.py — on-demand statement liquidity.

Mocks the network boundary (Typeform download + Claude vision), so the suite
stays hermetic.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from loan_officer.intake import statement_liquidity as sl


# ── aggregate_liquidity ──────────────────────────────────────────────────────

def test_aggregate_dedupes_same_account_across_months():
    per_doc = [
        {"ending_balance": 40_000, "bank_name": "Chase", "account_last4": "1234"},
        {"ending_balance": 42_000, "bank_name": "Chase", "account_last4": "1234"},
    ]
    out = sl.aggregate_liquidity(per_doc)
    assert out["liquid_assets"] == 42_000.0   # latest/largest, not 82k
    assert out["num_accounts"] == 1


def test_aggregate_sums_distinct_accounts():
    per_doc = [
        {"ending_balance": 40_000, "bank_name": "Chase", "account_last4": "1234"},
        {"ending_balance": 15_000, "bank_name": "BoA", "account_last4": "9999"},
    ]
    out = sl.aggregate_liquidity(per_doc)
    assert out["liquid_assets"] == 55_000.0
    assert out["num_accounts"] == 2


def test_aggregate_drops_implausible_balance():
    per_doc = [
        {"ending_balance": 99_999_999_999, "bank_name": "X", "account_last4": "1"},
        {"ending_balance": 30_000, "bank_name": "Y", "account_last4": "2"},
    ]
    out = sl.aggregate_liquidity(per_doc)
    assert out["liquid_assets"] == 30_000.0
    assert any(d.get("skipped_reason") for d in out["breakdown"])


# ── route ────────────────────────────────────────────────────────────────────

def test_ocr_statements_route_with_file_urls(app_client, auth_headers, temp_db):
    with patch.object(sl, "_download", return_value=(b"%PDF-mock", "application/pdf")), \
         patch.object(sl, "chat_with_vision",
                      return_value='{"ending_balance": 60000, "bank_name": "Chase", "account_last4": "1234"}'):
        resp = app_client.post(
            "/api/intake/ocr-statements",
            headers=auth_headers,
            json={"file_urls": ["https://api.typeform.com/a.pdf",
                                "https://api.typeform.com/b.pdf"]},
        )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    # Same account across two statements → not summed.
    assert body["liquid_assets"] == 60_000.0
    assert body["num_accounts"] == 1
    assert body["num_statements_ocrd"] == 2


def test_ocr_statements_route_requires_input(app_client, auth_headers, temp_db):
    resp = app_client.post("/api/intake/ocr-statements", headers=auth_headers, json={})
    assert resp.status_code == 400


def test_ocr_statements_route_503_without_token(app_client, auth_headers, temp_db, monkeypatch):
    monkeypatch.delenv("TYPEFORM_ACCESS_TOKEN", raising=False)
    resp = app_client.post(
        "/api/intake/ocr-statements",
        headers=auth_headers,
        json={"email": "borrower@example.com"},
    )
    assert resp.status_code == 503
    assert "TYPEFORM_ACCESS_TOKEN" in resp.get_json()["error"]


def test_ocr_statements_route_requires_auth(app_client):
    resp = app_client.post("/api/intake/ocr-statements", json={"email": "x@y.com"})
    assert resp.status_code in (401, 403)
