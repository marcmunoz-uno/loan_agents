"""
HTTP-layer tests for the tx_coordinator blueprint — auth, validation, 404s,
the upload size cap, and a happy-path open→get round trip. These cover the
Flask surface the pure-logic tests skip.
"""

from __future__ import annotations

import json

import pytest

from .conftest import TEST_SECRET


# ── auth ──────────────────────────────────────────────────────────────────────


def test_health_is_public(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_protected_route_rejects_missing_auth(client, insert_transaction):
    resp = client.get(f"/api/tx/{insert_transaction}")
    assert resp.status_code == 401


def test_protected_route_rejects_wrong_secret(client, insert_transaction):
    resp = client.get(
        f"/api/tx/{insert_transaction}",
        headers={"Authorization": "Bearer not-the-secret"},
    )
    assert resp.status_code == 401


def test_bearer_auth_accepted(client, insert_transaction, auth_headers):
    resp = client.get(f"/api/tx/{insert_transaction}", headers=auth_headers)
    assert resp.status_code == 200


def test_x_api_key_auth_accepted(client, insert_transaction):
    resp = client.get(
        f"/api/tx/{insert_transaction}",
        headers={"X-API-Key": TEST_SECRET},
    )
    assert resp.status_code == 200


def test_empty_bearer_does_not_match_empty_secret(client, insert_transaction, monkeypatch):
    """Guard against the degenerate '' == '' auth bypass if secret were blank."""
    from shared import auth as _auth
    monkeypatch.setattr(_auth, "TRANCHI_API_SECRET", "")
    resp = client.get(
        f"/api/tx/{insert_transaction}",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code == 401


# ── validation + not-found ──────────────────────────────────────────────────────


def test_get_unknown_transaction_404(client, auth_headers):
    resp = client.get("/api/tx/tx_does_not_exist", headers=auth_headers)
    assert resp.status_code == 404


def test_open_with_invalid_body_400(client, auth_headers):
    resp = client.post("/api/tx/open", headers=auth_headers, json={"purchase_price": "not-a-number"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Validation error"


def test_add_party_to_unknown_tx_404(client, auth_headers):
    resp = client.post(
        "/api/tx/tx_missing/party",
        headers=auth_headers,
        json={"party_type": "inspector", "name": "Inspector Gadget"},
    )
    assert resp.status_code == 404


def test_complete_unknown_milestone_404(client, insert_transaction, auth_headers):
    resp = client.post(
        f"/api/tx/{insert_transaction}/milestone/nonexistent_milestone/complete",
        headers=auth_headers,
        json={},
    )
    assert resp.status_code == 404


# ── happy path ──────────────────────────────────────────────────────────────────


def test_open_then_get_round_trip(client, auth_headers, sample_psa):
    body = json.loads(sample_psa.model_dump_json())
    body["user_id"] = "usr_test"
    resp = client.post("/api/tx/open", headers=auth_headers, json=body)
    assert resp.status_code == 201
    tx_id = resp.get_json()["tx_id"]
    assert tx_id.startswith("tx_")

    got = client.get(f"/api/tx/{tx_id}", headers=auth_headers)
    assert got.status_code == 200
    data = got.get_json()
    assert data["property_address"] == sample_psa.property_address
    assert data["status"] == "open"
    assert len(data["milestones"]) > 0


# ── upload cap ──────────────────────────────────────────────────────────────────


def test_oversized_upload_rejected(client, auth_headers):
    """MAX_CONTENT_LENGTH_MB=1 in the test fixture — a 2MB body should 413."""
    big = b"x" * (2 * 1024 * 1024)
    resp = client.post(
        "/api/tx/open-from-pdf",
        headers={**auth_headers, "Content-Type": "application/pdf"},
        data=big,
    )
    assert resp.status_code == 413
    assert resp.get_json()["error"] == "payload_too_large"


def test_empty_pdf_rejected(client, auth_headers):
    resp = client.post(
        "/api/tx/open-from-pdf",
        headers={**auth_headers, "Content-Type": "application/pdf"},
        data=b"",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "empty_pdf"


# ── boot guard ──────────────────────────────────────────────────────────────────


def test_assert_secret_configured_rejects_default(monkeypatch):
    from shared import auth as _auth
    monkeypatch.setattr(_auth, "TRANCHI_API_SECRET", "dev-secret-change-me")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError):
        _auth.assert_secret_configured()


def test_assert_secret_configured_allows_real_secret(monkeypatch):
    from shared import auth as _auth
    monkeypatch.setattr(_auth, "TRANCHI_API_SECRET", "a-real-secret")
    monkeypatch.delenv("APP_ENV", raising=False)
    _auth.assert_secret_configured()  # should not raise
