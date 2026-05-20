"""
loan_officer/tests/test_prequal_auto_letter.py — auto-fire prequal letter
from POST /api/loan/prequal.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

BORROWER = {
    "user_id": "usr_auto_test",
    "name": "Marc Munoz",
    "email": "marc@munoz.ltd",
    "phone": "(917) 981-0032",
    "credit_score": 740,
    "liquidity": 50000,
    "properties_owned": 3,
    "annual_income": 120000,
    "desired_loan_amount": 75000,
    "down_payment_pct": 25,
}
PROPERTY = {
    "address": "4521 Oak Ln Detroit MI 48224",
    "property_type": "single_family",
    "purchase_price": 100000,
    "monthly_rent": 1300,
    "annual_taxes": 2400,
    "annual_insurance": 1200,
}


def _post_prequal(app_client, auth_headers, body=None):
    return app_client.post("/api/loan/prequal", headers=auth_headers, json=body or {
        "borrower": BORROWER, "property": PROPERTY,
    })


def test_prequal_autofires_letter_by_default(app_client, temp_db, auth_headers, stub_s3, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://loan-agents.test")
    with patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        resp = _post_prequal(app_client, auth_headers)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["prequal_id"].startswith("pq_")
    assert "autofired_letter" in body, body
    af = body["autofired_letter"]
    assert af["letter_id"].startswith("pql_")
    assert af["max_pp_low"] > 0 and af["max_pp_high"] >= af["max_pp_low"]


def test_prequal_skip_letter_body_flag(app_client, temp_db, auth_headers, stub_s3, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://loan-agents.test")
    body = {"borrower": BORROWER, "property": PROPERTY, "skip_letter": True}
    with patch("loan_officer.prequal_letter.generate_and_send") as gs:
        resp = _post_prequal(app_client, auth_headers, body=body)
    assert resp.status_code == 201
    gs.assert_not_called()
    assert "autofired_letter" not in resp.get_json()


def test_prequal_skip_letter_env_flag(app_client, temp_db, auth_headers, monkeypatch):
    monkeypatch.setenv("LO_AUTO_FIRE_PREQUAL_LETTER", "0")
    with patch("loan_officer.prequal_letter.generate_and_send") as gs:
        resp = _post_prequal(app_client, auth_headers)
    assert resp.status_code == 201
    gs.assert_not_called()


def test_prequal_letter_failure_does_not_kill_prequal(app_client, temp_db, auth_headers, stub_s3, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://loan-agents.test")
    with patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.generate_and_send",
               side_effect=RuntimeError("Anthropic down")):
        resp = _post_prequal(app_client, auth_headers)
    # Prequal creation succeeded even though letter generation blew up
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["prequal_id"].startswith("pq_")
    af = body.get("autofired_letter", {})
    assert "error" in af
    assert "Anthropic down" in af["error"]
