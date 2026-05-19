"""
loan_officer/tests/test_inbound_email.py — POST /api/intake/inbound-email-attachment
+ PSA → deal-flow handoff.

No network: requests.get (Zapier attachment fetch) is patched; the DealFlowClient
HTTP call is patched at the requests.post boundary.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _seed_prequal(email="borrower@example.com", liquidity=30_000, name="Borrower"):
    """Seed a loan_prequals row matched on borrower email."""
    from shared.db import get_conn, insert
    now = "2026-05-19T00:00:00+00:00"
    pq_id = f"pq_test_{abs(hash(email)) % 10**10}"
    with get_conn() as conn:
        insert(conn, "loan_prequals", {
            "id":                       pq_id,
            "user_id":                  "usr_test",
            "borrower_data":            json.dumps({
                "user_id": "usr_test", "name": name, "email": email, "liquidity": liquidity,
            }),
            "property_data":            json.dumps({}),
            "score":                    72.0,
            "suggested_product":        "dscr",
            "dscr":                     1.05,
            "ltv":                      0.7,
            "monthly_payment_estimate": 500,
            "strengths":                "[]", "concerns": "[]", "next_steps": "[]",
            "status":                   "scored", "notes": "",
            "created_at":               now, "updated_at": now,
        })
    return pq_id


def _zapier_payload(
    sender="borrower@example.com",
    filename="PSA_signed.pdf",
    content_type="application/pdf",
    url="https://files.zapier.com/abc/PSA.pdf",
    message_id="<msg-1@gmail>",
    subject="Re: Your Pre-Qualification — Non-QM DSCR Loan",
    body_plain="",
):
    return {
        "from_email": sender,
        "subject": subject,
        "message_id": message_id,
        "body_plain": body_plain,
        "attachment_filename": filename,
        "attachment_url": url,
        "attachment_content_type": content_type,
    }


def _mock_attachment_get(content=b"%PDF-fake", content_type="application/pdf", status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.content = content
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status = MagicMock()
    return resp


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_inbound_endpoint_rejects_missing_auth(app_client, temp_db):
    resp = app_client.post("/api/intake/inbound-email-attachment", json=_zapier_payload())
    assert resp.status_code == 401


def test_inbound_endpoint_accepts_bearer(app_client, temp_db, monkeypatch, stub_s3):
    _seed_prequal()
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    # Re-import the module so it picks up the new env var
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    with patch("loan_officer.intake.routes.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value='{"doc_type":"bank_stmt","confidence":0.9,"extracted_fields":{},"warnings":[]}'):
        resp = app_client.post(
            "/api/intake/inbound-email-attachment",
            headers={"Authorization": "Bearer zap-secret", "Content-Type": "application/json"},
            json=_zapier_payload(),
        )
    assert resp.status_code == 200, resp.get_json()


def test_inbound_endpoint_accepts_x_zapier_secret_header(app_client, temp_db, monkeypatch, stub_s3):
    _seed_prequal()
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    with patch("loan_officer.intake.routes.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value='{"doc_type":"bank_stmt","confidence":0.9,"extracted_fields":{},"warnings":[]}'):
        resp = app_client.post(
            "/api/intake/inbound-email-attachment",
            headers={"X-Zapier-Secret": "zap-secret"},
            json=_zapier_payload(),
        )
    assert resp.status_code == 200


def test_inbound_endpoint_accepts_secret_query_param(app_client, temp_db, monkeypatch, stub_s3):
    _seed_prequal()
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    with patch("loan_officer.intake.routes.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value='{"doc_type":"bank_stmt","confidence":0.9,"extracted_fields":{},"warnings":[]}'):
        resp = app_client.post(
            "/api/intake/inbound-email-attachment?secret=zap-secret",
            json=_zapier_payload(),
        )
    assert resp.status_code == 200


# ── Borrower matching ────────────────────────────────────────────────────────

def test_inbound_no_match_returns_200_with_skip_reason(app_client, temp_db, monkeypatch, stub_s3):
    # No prequal seeded — no borrower to match
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    resp = app_client.post(
        "/api/intake/inbound-email-attachment",
        headers={"Authorization": "Bearer zap-secret"},
        json=_zapier_payload(sender="unknown@example.com"),
    )
    assert resp.status_code == 200  # ack to avoid Zapier retries
    assert resp.get_json()["skipped"] == "borrower_not_found"


def test_inbound_matches_by_letter_id_in_subject(app_client, temp_db, monkeypatch, stub_s3):
    """When the subject carries a pql_… reference, match by that even if the
    sender email is different (e.g., the borrower forwarded from another account)."""
    from shared.db import get_conn, insert
    pq_id = _seed_prequal(email="marc@munoz.ltd")
    # Add a letter row pointing at that prequal
    now = "2026-05-19T00:00:00+00:00"
    with get_conn() as conn:
        insert(conn, "prequal_letters", {
            "letter_id":          "pql_abc12345",
            "prequal_id":         pq_id,
            "application_id":     "",
            "borrower_name":      "Marc",
            "borrower_email":     "marc@munoz.ltd",
            "max_pp_low":         70000,
            "max_pp_high":        80000,
            "liquid_assets":      30000,
            "monthly_rent_used":  900,
            "rate_low_pct":       5.875,
            "rate_high_pct":      8.0,
            "down_pct_low":       20.0,
            "issued_at":          now,
            "expires_at":         now,
            "zap_fired":          0,
            "sent_to":            "",
            "breakdown":          "{}",
            "created_at":         now,
        })

    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    with patch("loan_officer.intake.routes.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value='{"doc_type":"bank_stmt","confidence":0.9,"extracted_fields":{},"warnings":[]}'):
        resp = app_client.post(
            "/api/intake/inbound-email-attachment",
            headers={"Authorization": "Bearer zap-secret"},
            json=_zapier_payload(
                sender="someoneelse@example.com",
                subject="Fwd: about pql_abc12345 prequal",
            ),
        )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["matched_via"] == "letter_id"
    assert body["prequal_id"] == pq_id


# ── Idempotency ──────────────────────────────────────────────────────────────

def test_inbound_dedups_on_message_id(app_client, temp_db, monkeypatch, stub_s3):
    _seed_prequal()
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    payload = _zapier_payload(message_id="<dedup-test>", filename="bank.pdf")
    with patch("loan_officer.intake.routes.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value='{"doc_type":"bank_stmt","confidence":0.9,"extracted_fields":{},"warnings":[]}'):
        first  = app_client.post("/api/intake/inbound-email-attachment",
                                 headers={"Authorization": "Bearer zap-secret"}, json=payload).get_json()
        second = app_client.post("/api/intake/inbound-email-attachment",
                                 headers={"Authorization": "Bearer zap-secret"}, json=payload).get_json()

    assert first["doc_id"]
    assert second["skipped"] == "already_processed"
    assert second["doc_id"] == first["doc_id"]


# ── S3 dependency ────────────────────────────────────────────────────────────

def test_inbound_returns_503_when_s3_not_configured(app_client, temp_db, monkeypatch):
    from shared.s3_client import S3Client
    _seed_prequal()
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)
    bare = S3Client(bucket="")

    with patch("loan_officer.intake.routes.get_default_client", return_value=bare), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()):
        resp = app_client.post(
            "/api/intake/inbound-email-attachment",
            headers={"Authorization": "Bearer zap-secret"},
            json=_zapier_payload(),
        )
    assert resp.status_code == 503


# ── PSA → TX handoff ──────────────────────────────────────────────────────────

def test_purchase_contract_classification_auto_opens_transaction(
    app_client, temp_db, monkeypatch, stub_s3,
):
    _seed_prequal()
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    monkeypatch.setenv("DEAL_FLOW_URL", "https://deal-flow.test")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    psa_vision = json.dumps({
        "doc_type": "purchase_contract",
        "confidence": 0.95,
        "extracted_fields": {
            "buyer_name":       "Marc Munoz",
            "seller_name":      "Jane Seller",
            "property_address": "4521 Oak Ln Detroit MI",
            "purchase_price":   "$95,000",
            "closing_date":     "06/20/2026",
        },
        "warnings": [],
    })

    fake_dealflow_resp = MagicMock()
    fake_dealflow_resp.status_code = 200
    fake_dealflow_resp.ok = True
    fake_dealflow_resp.json.return_value = {"tx_id": "tx_seed_auto_001"}

    with patch("loan_officer.intake.routes.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value=psa_vision), \
         patch("shared.deal_flow_client.requests.post",
               return_value=fake_dealflow_resp) as dealflow_post:
        resp = app_client.post(
            "/api/intake/inbound-email-attachment",
            headers={"Authorization": "Bearer zap-secret"},
            json=_zapier_payload(
                filename="PSA_signed.pdf",
                subject="Re: Your Pre-Qualification — under contract!",
                message_id="<psa-1>",
            ),
        )
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["classification"]["doc_type"] == "purchase_contract"
    assert "autofired_transaction" in body
    af = body["autofired_transaction"]
    assert af["tx_id"] == "tx_seed_auto_001"
    assert af["psa_terms"]["purchase_price"] == 95000.0
    assert af["psa_terms"]["closing_date"] == "2026-06-20"
    assert af["psa_terms"]["buyer_name"] == "Marc Munoz"

    # Deal-flow was POST'd with our standard headers
    args, kwargs = dealflow_post.call_args
    assert args[0] == "https://deal-flow.test/api/tx/open"
    assert kwargs["headers"]["Authorization"].startswith("Bearer ")
    assert kwargs["json"]["psa_terms"]["property_address"] == "4521 Oak Ln Detroit MI"


def test_psa_missing_required_field_does_not_open_tx(
    app_client, temp_db, monkeypatch, stub_s3,
):
    """If the classifier misses a required PSATerms field, skip TX open — don't error."""
    _seed_prequal()
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    psa_vision = json.dumps({
        "doc_type": "purchase_contract",
        "confidence": 0.7,
        "extracted_fields": {
            # Missing closing_date + property_address
            "buyer_name":     "Marc",
            "seller_name":    "Jane",
            "purchase_price": 95000,
        },
        "warnings": ["unclear handwriting"],
    })

    with patch("loan_officer.intake.routes.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value=psa_vision), \
         patch("shared.deal_flow_client.requests.post") as dealflow_post:
        resp = app_client.post(
            "/api/intake/inbound-email-attachment",
            headers={"Authorization": "Bearer zap-secret"},
            json=_zapier_payload(message_id="<psa-incomplete>"),
        )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["autofired_transaction"]["skipped"].startswith("missing_required_field")
    dealflow_post.assert_not_called()


def test_psa_dealflow_failure_surfaces_as_skip(
    app_client, temp_db, monkeypatch, stub_s3,
):
    """When deal-flow returns 5xx, the inbound endpoint still 200s + reports the failure."""
    _seed_prequal()
    monkeypatch.setenv("ZAPIER_INBOUND_SECRET", "zap-secret")
    import importlib, loan_officer.intake.routes as rr
    importlib.reload(rr)

    psa_vision = json.dumps({
        "doc_type": "purchase_contract",
        "confidence": 0.95,
        "extracted_fields": {
            "buyer_name":       "Marc",
            "seller_name":      "Jane",
            "property_address": "100 Test Ln",
            "purchase_price":   "95000",
            "closing_date":     "2026-06-20",
        },
        "warnings": [],
    })
    fail_resp = MagicMock()
    fail_resp.status_code = 503
    fail_resp.ok = False
    fail_resp.json.return_value = {"error": "down"}

    with patch("loan_officer.intake.routes.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.routes.requests.get",
               return_value=_mock_attachment_get()), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value=psa_vision), \
         patch("shared.deal_flow_client.requests.post", return_value=fail_resp):
        resp = app_client.post(
            "/api/intake/inbound-email-attachment",
            headers={"Authorization": "Bearer zap-secret"},
            json=_zapier_payload(message_id="<psa-flow-down>"),
        )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["autofired_transaction"]["skipped"].startswith("deal_flow_call_failed")


# ── Direct unit: deal_flow_client ─────────────────────────────────────────────

def test_dealflow_client_open_transaction_happy_path(monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "shared")
    import importlib
    import shared.deal_flow_client as dfc
    importlib.reload(dfc)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.ok = True
    fake_resp.json.return_value = {"tx_id": "tx_123"}

    with patch("shared.deal_flow_client.requests.post", return_value=fake_resp) as post:
        out = dfc.DealFlowClient(base_url="https://df.test").open_transaction(
            user_id="usr_x",
            psa_terms={"purchase_price": 100000, "closing_date": "2026-06-30",
                       "buyer_name": "A", "seller_name": "B", "property_address": "C"},
        )
    assert out["ok"] is True
    assert out["data"]["tx_id"] == "tx_123"
    args, kwargs = post.call_args
    assert args[0] == "https://df.test/api/tx/open"
    assert kwargs["headers"]["Authorization"] == "Bearer shared"


def test_dealflow_client_transport_failure():
    import shared.deal_flow_client as dfc
    import requests
    with patch("shared.deal_flow_client.requests.post",
               side_effect=requests.ConnectionError("refused")):
        out = dfc.DealFlowClient(base_url="https://df.test").open_transaction(
            user_id="u", psa_terms={"purchase_price": 1, "closing_date": "2026-01-01",
                                    "buyer_name": "A", "seller_name": "B", "property_address": "C"})
    assert out["ok"] is False
    assert "transport error" in out["error"]
