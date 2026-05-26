"""
loan_officer/typeform/tests/test_webhook.py — End-to-end webhook tests.

Run: python -m pytest loan_officer/typeform/tests/test_webhook.py -v
"""

import base64
import hashlib
import hmac
import json
import os
import tempfile
import uuid

import pytest

from app import create_app
from loan_officer.typeform.tests.test_mapper import _build_form_response


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def app(monkeypatch):
    # Isolated SQLite DB per test
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    monkeypatch.setenv("DB_PATH", tmp_db.name)

    # Clear Zapier creds so send_email returns skipped (no real network calls in tests).
    monkeypatch.delenv("ZAPIER_MCP_ENDPOINT", raising=False)
    monkeypatch.delenv("ZAPIER_MCP_API_KEY", raising=False)

    # Reload db module so the new DB_PATH takes effect, then create app.
    import importlib
    import shared.db
    importlib.reload(shared.db)
    import loan_officer.typeform.webhook as wh
    importlib.reload(wh)
    import app as app_module
    importlib.reload(app_module)

    app = app_module.create_app()
    yield app

    os.unlink(tmp_db.name)


@pytest.fixture
def client(app):
    return app.test_client()


SECRET = "test-typeform-secret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return "sha256=" + base64.b64encode(digest).decode()


def _payload(**form_kwargs) -> dict:
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:8]}",
        "event_type": "form_response",
        "form_response": _build_form_response(**form_kwargs),
    }


# ── Tests ────────────────────────────────────────────────────────────────────

class TestSignature:
    def test_rejects_missing_signature_when_secret_set(self, client, monkeypatch):
        monkeypatch.setenv("TYPEFORM_WEBHOOK_SECRET", SECRET)
        body = json.dumps(_payload()).encode()
        resp = client.post(
            "/api/loan/webhook/typeform-submit",
            data=body,
            content_type="application/json",
        )
        assert resp.status_code == 401

    def test_rejects_bad_signature(self, client, monkeypatch):
        monkeypatch.setenv("TYPEFORM_WEBHOOK_SECRET", SECRET)
        body = json.dumps(_payload()).encode()
        resp = client.post(
            "/api/loan/webhook/typeform-submit",
            data=body,
            content_type="application/json",
            headers={"Typeform-Signature": "sha256=" + base64.b64encode(b"nope").decode()},
        )
        assert resp.status_code == 401

    def test_accepts_valid_signature(self, client, monkeypatch):
        monkeypatch.setenv("TYPEFORM_WEBHOOK_SECRET", SECRET)
        body = json.dumps(_payload()).encode()
        resp = client.post(
            "/api/loan/webhook/typeform-submit",
            data=body,
            content_type="application/json",
            headers={"Typeform-Signature": _sign(body)},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["intake_id"].startswith("bi_")

    def test_passes_through_when_no_secret_configured(self, client, monkeypatch):
        # Dev mode — no secret env var, signature check skipped
        monkeypatch.delenv("TYPEFORM_WEBHOOK_SECRET", raising=False)
        body = json.dumps(_payload()).encode()
        resp = client.post(
            "/api/loan/webhook/typeform-submit",
            data=body,
            content_type="application/json",
        )
        assert resp.status_code == 200


class TestEndToEnd:
    def test_pass_intake_returns_pass_status(self, client, monkeypatch):
        monkeypatch.delenv("TYPEFORM_WEBHOOK_SECRET", raising=False)
        # Stub autofire — this test is about soft_prequal scoring + the
        # webhook returning 200 cleanly, not the letter pipeline. The
        # letter path has its own coverage below.
        import loan_officer.typeform.webhook as wh
        monkeypatch.setattr(wh, "fire_letter_async", lambda *a, **k: None)

        body = json.dumps(_payload(credit="780", all_docs=True)).encode()
        resp = client.post(
            "/api/loan/webhook/typeform-submit",
            data=body, content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["soft_prequal_status"] == "pass"
        # Asset statements uploaded → hand-off to autofire (stubbed here).
        assert data["email_send_status"] == "letter_pending"

    def test_intake_with_asset_statements_marks_letter_pending(self, client, monkeypatch):
        """When asset statements are uploaded, the webhook should hand off to
        the autofire thread and return immediately with status=letter_pending,
        not block on OCR or the letter pipeline."""
        monkeypatch.delenv("TYPEFORM_WEBHOOK_SECRET", raising=False)
        # Stub fire_letter_async so the test doesn't actually spawn a thread
        # that downloads URLs or calls Claude vision.
        import loan_officer.typeform.webhook as wh
        calls: list[tuple] = []
        monkeypatch.setattr(wh, "fire_letter_async", lambda iid, row: calls.append((iid, row)))

        body = json.dumps(_payload(credit="780", all_docs=True)).encode()
        resp = client.post(
            "/api/loan/webhook/typeform-submit",
            data=body, content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["email_send_status"] == "letter_pending"
        assert len(calls) == 1
        intake_id, intake_row = calls[0]
        assert intake_id.startswith("bi_")
        assert intake_row["asset_statement_recent_url"]

    def test_decline_intake_returns_decline_status(self, client, monkeypatch):
        monkeypatch.delenv("TYPEFORM_WEBHOOK_SECRET", raising=False)
        body = json.dumps(_payload(credit="580")).encode()
        resp = client.post(
            "/api/loan/webhook/typeform-submit",
            data=body, content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["soft_prequal_status"] == "decline"

    def test_duplicate_token_is_idempotent(self, client, monkeypatch):
        monkeypatch.delenv("TYPEFORM_WEBHOOK_SECRET", raising=False)
        body = json.dumps(_payload(credit="780")).encode()
        r1 = client.post("/api/loan/webhook/typeform-submit", data=body, content_type="application/json")
        r2 = client.post("/api/loan/webhook/typeform-submit", data=body, content_type="application/json")
        assert r1.status_code == 200
        assert r2.status_code == 200
        d1, d2 = r1.get_json(), r2.get_json()
        assert d2.get("duplicate") is True
        assert d2["intake_id"] == d1["intake_id"]

    def test_missing_form_response_returns_400(self, client, monkeypatch):
        monkeypatch.delenv("TYPEFORM_WEBHOOK_SECRET", raising=False)
        body = json.dumps({"event_id": "x"}).encode()
        resp = client.post(
            "/api/loan/webhook/typeform-submit",
            data=body, content_type="application/json",
        )
        assert resp.status_code == 400
