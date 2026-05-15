"""
loan_officer/tests/test_arive_zapier.py — Arive/Zapier integration tests.

Run: python -m pytest loan_officer/tests/test_arive_zapier.py -v

Tests run without real Zapier URLs — mock requests.post to verify behavior.
"""

import json
import os
import sys
import uuid
import hashlib
import hmac
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("DB_PATH", "data/test_arive.db")

from loan_officer.arive_zapier import fire_zap, to_arive_format, ARIVE_STATUS_MAP
from shared.db import init_db, get_conn, insert, fetchone
from shared.webhooks import sign_payload


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def setup_db():
    init_db()


def _make_full_record(app_id: str = "app_test_arive_001") -> dict:
    """Create a synthetic application + prequal row for testing."""
    borrower = {
        "user_id": "usr_test_arive",
        "name": "Alex Test Borrower",
        "email": "alex@test.com",
        "phone": "+13135550001",
        "credit_score": 720,
        "annual_income": 100_000,
        "liquidity": 50_000,
        "properties_owned": 2,
        "loan_purpose": "purchase",
        "desired_loan_amount": 71_250,
        "down_payment": 23_750,
        "down_payment_pct": 25,
    }
    prop = {
        "address": "4521 Oak Ln, Detroit MI 48224",
        "property_type": "single_family",
        "purchase_price": 95_000,
        "estimated_value": 110_000,
        "monthly_rent": 1_200,
        "annual_taxes": 2_400,
        "annual_insurance": 1_200,
        "hoa_monthly": 0,
    }
    return {
        "id": app_id,
        "borrower_data": json.dumps(borrower),
        "property_data": json.dumps(prop),
        "suggested_product": "dscr",
        "dscr": 1.18,
        "ltv": 0.648,
        "monthly_payment_estimate": 523.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: fire_zap posts to the right URL with correlation_id
# ─────────────────────────────────────────────────────────────────────────────

class TestFireZap:
    def test_skips_when_no_url_configured(self):
        """fire_zap is a no-op when ZAPIER_HOOK_PREQUAL_CREATED is empty."""
        # ZAPIER_HOOK_PREQUAL_CREATED defaults to "" in dev
        result = fire_zap("prequal_created", {"test": "payload"})
        assert result["success"] is False
        assert result.get("skipped") is True

    def test_posts_to_configured_url(self):
        """When a webhook URL is set, fire_zap POSTs to it."""
        test_url = "https://hooks.zapier.com/hooks/catch/test/prequal"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None

        with patch.dict("os.environ", {"ZAPIER_HOOK_PREQUAL_CREATED": test_url}):
            # Reload the module env
            from loan_officer import arive_zapier
            arive_zapier.ZAPIER_WEBHOOKS["prequal_created"] = test_url

            with patch("requests.post", return_value=mock_response) as mock_post:
                result = fire_zap("prequal_created", {"app_id": "app_test"})

                assert result["success"] is True
                mock_post.assert_called_once()
                call_args = mock_post.call_args
                assert call_args[0][0] == test_url

            # Restore
            arive_zapier.ZAPIER_WEBHOOKS["prequal_created"] = ""

    def test_correlation_id_injected_into_payload(self):
        """Payload sent to Zapier must contain correlation_id."""
        test_url = "https://hooks.zapier.com/hooks/catch/test/app"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None

        from loan_officer import arive_zapier
        arive_zapier.ZAPIER_WEBHOOKS["application_submitted"] = test_url

        with patch("requests.post", return_value=mock_response) as mock_post:
            fire_zap("application_submitted", {"app_id": "app_xyz"}, correlation_id="app_xyz")
            call_kwargs = mock_post.call_args[1]
            payload_sent = call_kwargs["json"]
            assert "correlation_id" in payload_sent
            assert payload_sent["correlation_id"] == "app_xyz"

        arive_zapier.ZAPIER_WEBHOOKS["application_submitted"] = ""

    def test_deterministic_correlation_id(self):
        """Same event_type + app_id should produce same correlation_id (idempotency)."""
        test_url = "https://hooks.zapier.com/hooks/catch/test/corr"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None

        from loan_officer import arive_zapier
        arive_zapier.ZAPIER_WEBHOOKS["prequal_created"] = test_url

        captured_ids = []
        with patch("requests.post", return_value=mock_response) as mock_post:
            for _ in range(2):
                fire_zap("prequal_created", {"prequal_id": "pq_dup_001"})
                payload_sent = mock_post.call_args[1]["json"]
                captured_ids.append(payload_sent["correlation_id"])

        assert captured_ids[0] == captured_ids[1], "Same inputs should produce same correlation_id"
        arive_zapier.ZAPIER_WEBHOOKS["prequal_created"] = ""

    def test_returns_failure_on_request_error(self):
        """Returns success=False (not raises) when POST fails."""
        import requests as req
        test_url = "https://hooks.zapier.com/hooks/catch/test/fail"

        from loan_officer import arive_zapier
        arive_zapier.ZAPIER_WEBHOOKS["lender_routed"] = test_url

        with patch("requests.post", side_effect=req.exceptions.ConnectionError("refused")):
            result = fire_zap("lender_routed", {"app_id": "app_fail"})
            assert result["success"] is False
            assert "error" in result

        arive_zapier.ZAPIER_WEBHOOKS["lender_routed"] = ""


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: to_arive_format produces expected fields for a DSCR loan
# ─────────────────────────────────────────────────────────────────────────────

class TestToAriveFormat:
    def test_required_fields_present(self):
        """All Arive-required fields must be present in the output."""
        record = _make_full_record()
        result = to_arive_format(record, correlation_id="pq_test_001")

        required_fields = [
            "loan_amount", "purchase_price", "down_payment", "down_payment_pct",
            "property_address", "property_city", "property_state", "property_zip",
            "borrower_first_name", "borrower_last_name", "borrower_email", "borrower_phone",
            "borrower_fico_estimate", "loan_purpose", "loan_product",
            "estimated_monthly_rent", "estimated_dscr",
            "mlo_assignment", "intake_source", "correlation_id",
        ]
        for field in required_fields:
            assert field in result, f"Missing required Arive field: {field}"

    def test_dscr_loan_product_label(self):
        record = _make_full_record()
        result = to_arive_format(record)
        assert result["loan_product"] == "DSCR", f"Expected 'DSCR', got '{result['loan_product']}'"

    def test_borrower_name_split(self):
        record = _make_full_record()
        result = to_arive_format(record)
        assert result["borrower_first_name"] == "Alex"
        assert result["borrower_last_name"] == "Test Borrower"

    def test_property_address_parsed(self):
        record = _make_full_record()
        result = to_arive_format(record)
        assert "4521 Oak Ln" in result["property_address"]
        assert result["property_state"] == "MI"

    def test_loan_purpose_normalized(self):
        record = _make_full_record()
        result = to_arive_format(record)
        assert result["loan_purpose"] == "purchase"

    def test_cash_out_refi_normalized(self):
        record = _make_full_record()
        borrower = json.loads(record["borrower_data"])
        borrower["loan_purpose"] = "cash_out_refi"
        record["borrower_data"] = json.dumps(borrower)
        result = to_arive_format(record)
        assert result["loan_purpose"] == "cash_out"

    def test_intake_source_is_tranchi(self):
        record = _make_full_record()
        result = to_arive_format(record)
        assert result["intake_source"] == "tranchi.ai"

    def test_correlation_id_passed_through(self):
        record = _make_full_record()
        result = to_arive_format(record, correlation_id="my_custom_corr_id")
        assert result["correlation_id"] == "my_custom_corr_id"

    def test_fix_flip_product_label(self):
        record = _make_full_record()
        record["suggested_product"] = "fix_flip"
        result = to_arive_format(record)
        assert result["loan_product"] == "Fix & Flip"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Inbound Arive webhook updates application state
# ─────────────────────────────────────────────────────────────────────────────

class TestAriveInboundWebhook:
    """Test that POST /api/loan/webhook/arive-update updates application state."""

    def setup_method(self):
        """Insert a test application in UNDERWRITING state."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        self.app_id = "app_test_arive_inbound_001"
        self.prequal_id = "pq_test_arive_inbound_001"

        with get_conn() as conn:
            # Clean up prior runs — must delete children before parents (FK constraints)
            conn.execute("DELETE FROM pre_underwriting_reports WHERE application_id = ?", (self.app_id,))
            conn.execute("DELETE FROM loan_audit_log WHERE application_id = ?", (self.app_id,))
            conn.execute("DELETE FROM loan_documents WHERE application_id = ?", (self.app_id,))
            conn.execute("DELETE FROM loan_applications WHERE id = ?", (self.app_id,))
            conn.execute("DELETE FROM loan_prequals WHERE id = ?", (self.prequal_id,))
            conn.commit()

            insert(conn, "loan_prequals", {
                "id": self.prequal_id,
                "user_id": "usr_test_arive",
                "borrower_data": "{}",
                "property_data": "{}",
                "score": 75.0,
                "suggested_product": "dscr",
                "dscr": 1.18,
                "ltv": 0.648,
                "monthly_payment_estimate": 523.0,
                "strengths": "[]",
                "concerns": "[]",
                "next_steps": "[]",
                "status": "scored",
                "notes": "test",
                "created_at": now,
                "updated_at": now,
            })
            insert(conn, "loan_applications", {
                "id": self.app_id,
                "prequal_id": self.prequal_id,
                "user_id": "usr_test_arive",
                "status": "UNDERWRITING",
                "current_state": "UNDERWRITING",
                "lender_partner": "kiavi",
                "lender_ref_id": "STUB-KIAVI-TESTINBOUND",
                "docs_required": "[]",
                "docs_received": "[]",
                "underwriter_notes": "",
                "approved_amount": None,
                "approved_rate": None,
                "approved_term": None,
                "conditions": "[]",
                "audit_log": "[]",
                "created_at": now,
                "updated_at": now,
            })

    def _make_flask_client(self):
        """Create a Flask test client with the app configured."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from app import create_app
        flask_app = create_app()
        flask_app.config["TESTING"] = True
        return flask_app.test_client()

    def _sign(self, body: bytes, secret: str) -> str:
        return hmac.new(secret.encode(), body, "sha256").hexdigest()  # type: ignore[attr-defined]

    def test_arive_webhook_clears_to_close(self):
        """Arive 'Cleared to Close' maps to CLOSING state."""
        client = self._make_flask_client()
        payload = {
            "correlation_id": self.app_id,
            "event_type": "status_change",
            "status": "Cleared to Close",
            "conditions": [],
            "notes": "UW cleared all conditions.",
        }
        body = json.dumps(payload).encode()
        # No secret configured in test env — ARIVE_WEBHOOK_SECRET defaults to ""
        resp = client.post(
            "/api/loan/webhook/arive-update",
            data=body,
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["received"] is True
        assert data["application_id"] == self.app_id

    def test_arive_webhook_maps_status_correctly(self):
        """Verify the ARIVE_STATUS_MAP covers key status strings."""
        assert ARIVE_STATUS_MAP.get("Cleared to Close") == "CLOSING"
        assert ARIVE_STATUS_MAP.get("Funded") == "FUNDED"
        assert ARIVE_STATUS_MAP.get("Declined") == "DECLINED"
        assert ARIVE_STATUS_MAP.get("Submitted to Underwriting") == "UNDERWRITING"

    def test_arive_webhook_requires_correlation_id(self):
        """Webhook returns 400 if correlation_id is missing."""
        client = self._make_flask_client()
        resp = client.post(
            "/api/loan/webhook/arive-update",
            data=json.dumps({"status": "Funded"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_arive_webhook_unknown_app_returns_404(self):
        """Webhook returns 404 for unknown correlation_id."""
        client = self._make_flask_client()
        resp = client.post(
            "/api/loan/webhook/arive-update",
            data=json.dumps({
                "correlation_id": "app_does_not_exist_xyz",
                "status": "Funded",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 404

    def test_hmac_signature_verified_when_secret_set(self):
        """When ARIVE_WEBHOOK_SECRET is set, invalid HMAC returns 401."""
        import app as main_app
        flask_app = main_app.create_app()
        flask_app.config["TESTING"] = True

        with patch.dict("os.environ", {"ARIVE_WEBHOOK_SECRET": "test_secret_123"}):
            with flask_app.test_client() as client:
                payload = json.dumps({
                    "correlation_id": self.app_id,
                    "status": "Cleared to Close",
                }).encode()

                # Wrong signature
                resp = client.post(
                    "/api/loan/webhook/arive-update",
                    data=payload,
                    content_type="application/json",
                    headers={"X-Arive-Signature": "wrong_signature"},
                )
                assert resp.status_code == 401

    def test_hmac_valid_signature_passes(self):
        """Correct HMAC signature is accepted."""
        import app as main_app
        flask_app = main_app.create_app()
        flask_app.config["TESTING"] = True
        secret = "test_secret_valid"

        with patch.dict("os.environ", {"ARIVE_WEBHOOK_SECRET": secret}):
            with flask_app.test_client() as client:
                payload_dict = {
                    "correlation_id": self.app_id,
                    "status": "Cleared to Close",
                }
                payload = json.dumps(payload_dict).encode()
                sig = sign_payload(payload, secret)

                resp = client.post(
                    "/api/loan/webhook/arive-update",
                    data=payload,
                    content_type="application/json",
                    headers={"X-Arive-Signature": sig},
                )
                assert resp.status_code == 200
