"""
loan_officer/tests/test_arive_new_loan.py — Arive "New Loan" webhook read-back.

Covers the new event branch in POST /api/loan/webhook/arive-update that fires
when the borrower self-registers + submits the 1003 on my1003app. Webhook auth
uses ARIVE_WEBHOOK_SECRET (verify_webhook decorator).
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from shared.db import get_conn, insert, fetchone


def _seed_prequal(prequal_id="pq_arive_test_1", email="borrower@example.com"):
    now = "2026-05-19T00:00:00+00:00"
    with get_conn() as conn:
        insert(conn, "loan_prequals", {
            "id": prequal_id,
            "user_id": "usr_arive_test",
            "borrower_data": json.dumps({
                "user_id": "usr_arive_test",
                "name": "Test Borrower",
                "email": email,
            }),
            "property_data": json.dumps({}),
            "score": 72.0,
            "suggested_product": "dscr",
            "dscr": 1.05, "ltv": 0.7, "monthly_payment_estimate": 500,
            "strengths": "[]", "concerns": "[]", "next_steps": "[]",
            "status": "scored", "notes": "",
            "created_at": now, "updated_at": now,
        })
    return prequal_id


def _seed_application(application_id, prequal_id, lender_ref_id=""):
    now = "2026-05-19T00:00:00+00:00"
    with get_conn() as conn:
        insert(conn, "loan_applications", {
            "id": application_id, "prequal_id": prequal_id, "user_id": "usr_arive_test",
            "status": "APP_STARTED", "current_state": "APP_STARTED",
            "lender_partner": "", "lender_ref_id": lender_ref_id,
            "docs_required": "[]", "docs_received": "[]",
            "underwriter_notes": "", "approved_amount": None,
            "approved_rate": None, "approved_term": None,
            "conditions": "[]", "audit_log": "[]",
            "created_at": now, "updated_at": now,
        })


def _sign(body: dict, secret: str = "arive-test-secret") -> str:
    raw = json.dumps(body, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _arive_payload(arive_loan_id=14746334, email="borrower@example.com", crm_ref="", **extra):
    payload = {
        "event_type": "new_loan",
        "ariveLoanId": arive_loan_id,
        "loanBorrower1_firstName": "Test",
        "loanBorrower1_lastName": "Borrower",
        "loanBorrower1_emailAddressText": email,
        "currentLoanStatus_status": "APPLICATION_INTAKE",
        "deepLinkURL": f"https://munoz.myarive.com/app/loans/{arive_loan_id}",
    }
    if crm_ref:
        payload["crmReferenceId"] = crm_ref
    payload.update(extra)
    return payload


# ── Matching by crmReferenceId ────────────────────────────────────────────────

def test_new_loan_matches_by_crm_reference_id(app_client, temp_db, monkeypatch):
    monkeypatch.setenv("ARIVE_WEBHOOK_SECRET", "arive-test-secret")
    pq_id = _seed_prequal(prequal_id="pq_arive_crm_1")
    payload = _arive_payload(crm_ref="pq_arive_crm_1", email="other@example.com")
    body = json.dumps(payload, separators=(",", ":"))
    resp = app_client.post(
        "/api/loan/webhook/arive-update",
        data=body, content_type="application/json",
        headers={"X-Arive-Signature": _sign(payload)},
    )
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out["matched"] is True
    assert out["matched_via"] == "crm_reference_id"
    assert out["prequal_id"] == pq_id
    assert out["arive_loan_id"] == "14746334"
    # Application got created with the Arive loan id on lender_ref_id
    with get_conn() as conn:
        app_row = fetchone(conn, "SELECT * FROM loan_applications WHERE id = ?", (out["application_id"],))
    assert app_row["lender_ref_id"] == "14746334"
    assert app_row["current_state"] == "APP_SUBMITTED"


# ── Matching by borrower email ────────────────────────────────────────────────

def test_new_loan_matches_by_borrower_email(app_client, temp_db, monkeypatch):
    monkeypatch.setenv("ARIVE_WEBHOOK_SECRET", "arive-test-secret")
    _seed_prequal(prequal_id="pq_arive_email_1", email="borrower@example.com")
    payload = _arive_payload(email="borrower@example.com")  # no crm_ref
    body = json.dumps(payload, separators=(",", ":"))
    resp = app_client.post(
        "/api/loan/webhook/arive-update",
        data=body, content_type="application/json",
        headers={"X-Arive-Signature": _sign(payload)},
    )
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()
    assert out["matched"] is True
    assert out["matched_via"] == "borrower_email"


# ── No match ──────────────────────────────────────────────────────────────────

def test_new_loan_no_match_returns_200_with_reason(app_client, temp_db, monkeypatch):
    monkeypatch.setenv("ARIVE_WEBHOOK_SECRET", "arive-test-secret")
    # No prequal seeded
    payload = _arive_payload(email="nobody@example.com")
    body = json.dumps(payload, separators=(",", ":"))
    resp = app_client.post(
        "/api/loan/webhook/arive-update",
        data=body, content_type="application/json",
        headers={"X-Arive-Signature": _sign(payload)},
    )
    assert resp.status_code == 200
    out = resp.get_json()
    assert out["matched"] is False
    assert "no prequal" in out["reason"].lower()


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_new_loan_dedup_when_already_linked(app_client, temp_db, monkeypatch):
    """Re-firing the same Arive loan ID for the same application should be a no-op."""
    monkeypatch.setenv("ARIVE_WEBHOOK_SECRET", "arive-test-secret")
    _seed_prequal(prequal_id="pq_arive_dedup_1")
    _seed_application("app_arive_dedup_1", "pq_arive_dedup_1", lender_ref_id="14746334")

    payload = _arive_payload(crm_ref="pq_arive_dedup_1", arive_loan_id=14746334)
    body = json.dumps(payload, separators=(",", ":"))
    resp = app_client.post(
        "/api/loan/webhook/arive-update",
        data=body, content_type="application/json",
        headers={"X-Arive-Signature": _sign(payload)},
    )
    out = resp.get_json()
    assert resp.status_code == 200
    assert out["matched"] is True
    assert out.get("deduped") is True


# ── Auto-detect event_type when missing ───────────────────────────────────────

def test_new_loan_auto_detects_by_presence_of_arive_loan_id(app_client, temp_db, monkeypatch):
    """If `event_type` is missing but `ariveLoanId` is in the body, route to new_loan path."""
    monkeypatch.setenv("ARIVE_WEBHOOK_SECRET", "arive-test-secret")
    _seed_prequal(prequal_id="pq_arive_autodetect_1")
    payload = _arive_payload(crm_ref="pq_arive_autodetect_1")
    payload.pop("event_type")
    body = json.dumps(payload, separators=(",", ":"))
    resp = app_client.post(
        "/api/loan/webhook/arive-update",
        data=body, content_type="application/json",
        headers={"X-Arive-Signature": _sign(payload)},
    )
    out = resp.get_json()
    assert resp.status_code == 200
    assert out["matched"] is True


# ── Bad payloads ──────────────────────────────────────────────────────────────

def test_new_loan_missing_arive_loan_id_returns_400(app_client, temp_db, monkeypatch):
    monkeypatch.setenv("ARIVE_WEBHOOK_SECRET", "arive-test-secret")
    payload = {"event_type": "new_loan", "loanBorrower1_emailAddressText": "x@y"}
    body = json.dumps(payload, separators=(",", ":"))
    resp = app_client.post(
        "/api/loan/webhook/arive-update",
        data=body, content_type="application/json",
        headers={"X-Arive-Signature": _sign(payload)},
    )
    assert resp.status_code == 400
    assert "arive_loan_id" in resp.get_json()["error"].lower() or \
           "ariveLoanId" in resp.get_json()["error"]


def test_new_loan_rejects_bad_signature(app_client, temp_db, monkeypatch):
    monkeypatch.setenv("ARIVE_WEBHOOK_SECRET", "arive-test-secret")
    _seed_prequal(prequal_id="pq_arive_badsig")
    payload = _arive_payload(crm_ref="pq_arive_badsig")
    body = json.dumps(payload, separators=(",", ":"))
    resp = app_client.post(
        "/api/loan/webhook/arive-update",
        data=body, content_type="application/json",
        headers={"X-Arive-Signature": "00" * 32},
    )
    assert resp.status_code in (401, 403)
