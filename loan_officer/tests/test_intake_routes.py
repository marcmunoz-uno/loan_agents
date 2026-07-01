"""
loan_officer/tests/test_intake_routes.py — Flask blueprint smoke tests.

Patches the upload + classifier modules so each route call exercises real
Flask routing + auth + JSON serialization, but never touches S3 or Anthropic.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from shared.s3_client import PresignedUpload, S3NotConfigured


# ── /upload/presign ───────────────────────────────────────────────────────────

def test_presign_happy_path(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        resp = app_client.post("/api/intake/upload/presign",
                               headers=auth_headers,
                               json={"deal_id": "d1", "filename": "x.pdf",
                                     "content_type": "application/pdf",
                                     "user_id": "u1", "declared_doc_type": "bank_stmt"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["doc_id"].startswith("doc_")
    assert body["status"] == "presigned"


def test_presign_400_on_missing_filename(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        resp = app_client.post("/api/intake/upload/presign",
                               headers=auth_headers,
                               json={"deal_id": "d1", "filename": "",
                                     "content_type": "application/pdf"})
    assert resp.status_code == 400


def test_presign_503_when_s3_unconfigured(app_client, auth_headers):
    from shared.s3_client import S3Client
    bare = S3Client(bucket="")
    with patch("loan_officer.intake.upload.get_default_client", return_value=bare):
        resp = app_client.post("/api/intake/upload/presign",
                               headers=auth_headers,
                               json={"deal_id": "d1", "filename": "x.pdf",
                                     "content_type": "application/pdf"})
    assert resp.status_code == 503


def test_presign_requires_auth(app_client, stub_s3):
    resp = app_client.post("/api/intake/upload/presign",
                           json={"deal_id": "d", "filename": "x.pdf",
                                 "content_type": "application/pdf"})
    assert resp.status_code in (401, 403)


# ── /upload/confirm ───────────────────────────────────────────────────────────

def test_confirm_flow(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        p = app_client.post("/api/intake/upload/presign",
                            headers=auth_headers,
                            json={"deal_id": "d", "filename": "x.pdf",
                                  "content_type": "application/pdf"}).get_json()
        resp = app_client.post("/api/intake/upload/confirm",
                               headers=auth_headers,
                               json={"doc_id": p["doc_id"], "size_bytes": 12345})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "uploaded"


def test_confirm_404_on_unknown_doc(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        resp = app_client.post("/api/intake/upload/confirm",
                               headers=auth_headers,
                               json={"doc_id": "doc_nope", "verify_in_s3": False})
    assert resp.status_code == 404


def test_confirm_400_on_missing_doc_id(app_client, auth_headers):
    resp = app_client.post("/api/intake/upload/confirm",
                           headers=auth_headers, json={})
    assert resp.status_code == 400


# ── /upload/<doc_id>/classify ─────────────────────────────────────────────────

def test_classify_route_calls_classifier(app_client, auth_headers, stub_s3):
    payload = '{"doc_type": "bank_stmt", "confidence": 0.9, "extracted_fields": {"bank_name": "Chase"}, "warnings": []}'
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision", return_value=payload):
        p = app_client.post("/api/intake/upload/presign",
                            headers=auth_headers,
                            json={"deal_id": "d", "filename": "x.pdf",
                                  "content_type": "application/pdf"}).get_json()
        app_client.post("/api/intake/upload/confirm",
                        headers=auth_headers,
                        json={"doc_id": p["doc_id"]})
        resp = app_client.post(f"/api/intake/upload/{p['doc_id']}/classify",
                               headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["doc_type"] == "bank_stmt"
    assert body["confidence"] == 0.9


def test_classify_404_on_unknown(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3):
        resp = app_client.post("/api/intake/upload/doc_nope/classify",
                               headers=auth_headers)
    assert resp.status_code == 404


def test_classify_409_when_not_uploaded(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3):
        p = app_client.post("/api/intake/upload/presign",
                            headers=auth_headers,
                            json={"deal_id": "d", "filename": "x.pdf",
                                  "content_type": "application/pdf"}).get_json()
        resp = app_client.post(f"/api/intake/upload/{p['doc_id']}/classify",
                               headers=auth_headers)
    assert resp.status_code == 409


def test_classify_502_on_vision_failure(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               side_effect=RuntimeError("API down")):
        p = app_client.post("/api/intake/upload/presign",
                            headers=auth_headers,
                            json={"deal_id": "d", "filename": "x.pdf",
                                  "content_type": "application/pdf"}).get_json()
        app_client.post("/api/intake/upload/confirm",
                        headers=auth_headers,
                        json={"doc_id": p["doc_id"]})
        resp = app_client.post(f"/api/intake/upload/{p['doc_id']}/classify",
                               headers=auth_headers)
    assert resp.status_code == 502


# ── /upload/<doc_id> (status) + /attach ───────────────────────────────────────

def test_status_returns_row(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        p = app_client.post("/api/intake/upload/presign",
                            headers=auth_headers,
                            json={"deal_id": "d", "filename": "x.pdf",
                                  "content_type": "application/pdf"}).get_json()
        resp = app_client.get(f"/api/intake/upload/{p['doc_id']}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "presigned"


def test_status_404_on_unknown(app_client, auth_headers):
    resp = app_client.get("/api/intake/upload/doc_nope", headers=auth_headers)
    assert resp.status_code == 404


def test_attach_to_application(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        p = app_client.post("/api/intake/upload/presign",
                            headers=auth_headers,
                            json={"deal_id": "d", "filename": "x.pdf",
                                  "content_type": "application/pdf"}).get_json()
        resp = app_client.post(f"/api/intake/upload/{p['doc_id']}/attach",
                               headers=auth_headers,
                               json={"application_id": "app_42"})
    assert resp.status_code == 200
    assert resp.get_json()["application_id"] == "app_42"


def test_attach_requires_application_id(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        p = app_client.post("/api/intake/upload/presign",
                            headers=auth_headers,
                            json={"deal_id": "d", "filename": "x.pdf",
                                  "content_type": "application/pdf"}).get_json()
        resp = app_client.post(f"/api/intake/upload/{p['doc_id']}/attach",
                               headers=auth_headers, json={})
    assert resp.status_code == 400


# ── Listing endpoints ─────────────────────────────────────────────────────────

def test_docs_by_deal(app_client, auth_headers, stub_s3):
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        app_client.post("/api/intake/upload/presign", headers=auth_headers,
                        json={"deal_id": "d99", "filename": "a.pdf",
                              "content_type": "application/pdf"})
        resp = app_client.get("/api/intake/deals/d99/docs", headers=auth_headers)
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["deal_id"] == "d99"
    assert len(body["docs"]) == 1


# ── Completeness ──────────────────────────────────────────────────────────────

def test_completeness_uses_classified_doc_type(app_client, auth_headers, stub_s3):
    """
    Two docs uploaded + one classified → completeness counts the classified one
    against the dscr checklist.
    """
    vision_payloads = iter([
        '{"doc_type": "bank_stmt", "confidence": 0.95, "extracted_fields": {}, "warnings": []}',
    ])
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               side_effect=lambda *a, **k: next(vision_payloads)):
        # Upload one doc and classify it
        p = app_client.post("/api/intake/upload/presign", headers=auth_headers,
                            json={"deal_id": "d", "filename": "bs.pdf",
                                  "content_type": "application/pdf",
                                  "application_id": "app_55"}).get_json()
        app_client.post("/api/intake/upload/confirm", headers=auth_headers,
                        json={"doc_id": p["doc_id"]})
        app_client.post(f"/api/intake/upload/{p['doc_id']}/classify", headers=auth_headers)

        # Hit completeness — only bank_stmt is present out of dscr's 3 required
        resp = app_client.get("/api/intake/applications/app_55/completeness?product=dscr",
                              headers=auth_headers)
    assert resp.status_code == 200
    body = resp.get_json()
    assert "bank_stmt" in body["received"]
    assert "rent_roll" in body["required_missing"]
    assert "purchase_contract" in body["required_missing"]
    assert body["is_complete"] is False
    assert body["completion_pct"] < 100


# ── Self-hosted PDF endpoint (/api/loan/prequal-letter/<id>/pdf) ──────────────

def test_letter_pdf_endpoint_serves_pdf_with_valid_token(app_client, temp_db, stub_s3, monkeypatch):
    from shared.db import get_conn, insert
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-secret")  # matches conftest auth fixture

    now = "2026-05-19T00:00:00+00:00"
    # FK requires loan_prequals.id to exist before we can insert into prequal_letters
    with get_conn() as conn:
        insert(conn, "loan_prequals", {
            "id":                       "pq_x_letter",
            "user_id":                  "usr_marc",
            "borrower_data":            '{"name":"Test","email":"t@example.com"}',
            "property_data":            '{}',
            "score":                    0,
            "suggested_product":        "dscr",
            "dscr":                     1.0,
            "ltv":                      0.7,
            "monthly_payment_estimate": 0,
            "strengths":                "[]", "concerns": "[]", "next_steps": "[]",
            "status":                   "scored", "notes": "",
            "created_at":               now, "updated_at": now,
        })
        insert(conn, "prequal_letters", {
            "letter_id":          "pql_self_1",
            "prequal_id":         "pq_x_letter",
            "application_id":     "",
            "borrower_name":      "Test Borrower",
            "borrower_email":     "test@example.com",
            "max_pp_low":         70000,
            "max_pp_high":        85000,
            "liquid_assets":      24000,
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

    from loan_officer.prequal_letter import sign_letter_pdf_token
    import time
    exp = int(time.time()) + 3600
    token = sign_letter_pdf_token("pql_self_1", exp)

    resp = app_client.get(f"/api/loan/prequal-letter/pql_self_1/pdf?token={token}&exp={exp}")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF-")
    assert "attachment" in resp.headers.get("Content-Disposition", "")


def test_letter_pdf_endpoint_rejects_bad_token(app_client, temp_db, monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-secret")
    import time
    exp = int(time.time()) + 3600
    resp = app_client.get(f"/api/loan/prequal-letter/pql_x/pdf?token={'00' * 16}&exp={exp}")
    assert resp.status_code == 403


def test_letter_pdf_endpoint_rejects_expired_token(app_client, temp_db, monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-secret")
    from loan_officer.prequal_letter import sign_letter_pdf_token
    import time
    exp = int(time.time()) - 1  # expired
    token = sign_letter_pdf_token("pql_x", exp)
    resp = app_client.get(f"/api/loan/prequal-letter/pql_x/pdf?token={token}&exp={exp}")
    assert resp.status_code == 403


def test_letter_pdf_endpoint_404_when_letter_missing(app_client, temp_db, monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-secret")
    from loan_officer.prequal_letter import sign_letter_pdf_token
    import time
    exp = int(time.time()) + 3600
    token = sign_letter_pdf_token("pql_missing", exp)
    resp = app_client.get(f"/api/loan/prequal-letter/pql_missing/pdf?token={token}&exp={exp}")
    assert resp.status_code == 404


def test_letter_pdf_endpoint_does_not_require_bearer_auth(app_client, temp_db, monkeypatch):
    """The /pdf endpoint is intentionally accessible without Authorization,
    auth is via the URL-signed token. Anything pulling the URL (Zapier, Gmail
    preview) can fetch it."""
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-secret")
    from loan_officer.prequal_letter import sign_letter_pdf_token
    import time
    exp = int(time.time()) + 3600
    token = sign_letter_pdf_token("pql_anon", exp)
    # No Authorization header at all
    resp = app_client.get(f"/api/loan/prequal-letter/pql_anon/pdf?token={token}&exp={exp}")
    # 404 (letter doesn't exist) — confirms the route reached the body, not 401
    assert resp.status_code == 404


# ── Auto-fire prequal letter from /classify ───────────────────────────────────

def _seed_prequal_and_app(app_id="app_autofire_1", prequal_id="pq_autofire_1"):
    """Seed a prequal + application so the autofire path has somewhere to land."""
    from shared.db import get_conn, insert
    now = "2026-05-19T00:00:00+00:00"
    with get_conn() as conn:
        insert(conn, "loan_prequals", {
            "id":                       prequal_id,
            "user_id":                  "usr_marc",
            "borrower_data":            '{"user_id":"usr_marc","name":"Maya","email":"maya@example.com","liquidity":25000}',
            "property_data":            '{"address":"100 Test Ln","property_type":"single_family","monthly_rent":900,"purchase_price":80000,"annual_taxes":2000,"annual_insurance":1000}',
            "score":                    72.0,
            "suggested_product":        "dscr",
            "dscr":                     1.05,
            "ltv":                      0.7,
            "monthly_payment_estimate": 500,
            "strengths":                "[]", "concerns": "[]", "next_steps": "[]",
            "status":                   "scored", "notes": "",
            "created_at":               now, "updated_at": now,
        })
        insert(conn, "loan_applications", {
            "id":                app_id,
            "prequal_id":        prequal_id,
            "user_id":           "usr_marc",
            "status":            "APP_DOCS_PENDING",
            "current_state":     "APP_DOCS_PENDING",
            "lender_partner":    "",
            "lender_ref_id":     "",
            "docs_required":     "[]",
            "docs_received":     "[]",
            "underwriter_notes": "",
            "approved_amount":   None,
            "approved_rate":     None,
            "approved_term":     None,
            "conditions":        "[]",
            "audit_log":         "[]",
            "created_at":        now, "updated_at": now,
        })


def _upload_classified_doc(app_client, auth_headers, stub_s3, app_id, declared_type, classify=True):
    """Helper: presign → confirm, optionally stamping a successful classification.

    The autofire gate only counts docs that were genuinely CLASSIFIED as their
    type with high confidence (never the borrower's self-declared label), so by
    default the helper records a classified_doc_type + confidence to model a real
    intake. Pass classify=False to leave the doc in 'uploaded' state when the test
    will classify it through the /classify endpoint (which triggers autofire).
    """
    p = app_client.post("/api/intake/upload/presign", headers=auth_headers,
                        json={"deal_id": "d", "filename": f"{declared_type}.pdf",
                              "content_type": "application/pdf",
                              "application_id": app_id,
                              "declared_doc_type": declared_type}).get_json()
    app_client.post("/api/intake/upload/confirm", headers=auth_headers,
                    json={"doc_id": p["doc_id"]})
    if classify:
        from shared.db import get_conn, update
        with get_conn() as conn:
            update(conn, "intake_documents",
                   {"classified_doc_type": declared_type, "confidence": 0.95, "status": "classified"},
                   "doc_id = ?", (p["doc_id"],))
    return p["doc_id"]


def test_classify_autofires_letter_when_dscr_checklist_complete(app_client, auth_headers, stub_s3):
    """The DSCR checklist requires bank_stmt + rent_roll + purchase_contract.
    After classifying the final required doc, the letter should auto-generate."""
    _seed_prequal_and_app()
    fake_mcp = type("MCP", (), {"configured": False, "execute": lambda self, **kw: {}})()

    vision_payload = '{"doc_type": "bank_stmt", "confidence": 0.95, "extracted_fields": {"ending_balance": 25000}, "warnings": []}'

    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision", return_value=vision_payload), \
         patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.ZapierMCPClient", return_value=fake_mcp), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        # Upload rent_roll + purchase_contract upfront
        _upload_classified_doc(app_client, auth_headers, stub_s3, "app_autofire_1", "rent_roll")
        _upload_classified_doc(app_client, auth_headers, stub_s3, "app_autofire_1", "purchase_contract")
        bank_doc = _upload_classified_doc(app_client, auth_headers, stub_s3, "app_autofire_1", "bank_stmt", classify=False)
        resp = app_client.post(f"/api/intake/upload/{bank_doc}/classify", headers=auth_headers)

    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["doc_type"] == "bank_stmt"
    assert "autofired_letter" in body, body
    af = body["autofired_letter"]
    assert af["letter_id"].startswith("pql_")
    assert af["max_pp_low"] > 0
    assert af["max_pp_high"] >= af["max_pp_low"]


def test_classify_no_autofire_when_checklist_incomplete(app_client, auth_headers, stub_s3):
    """Classifying one doc when the checklist still needs others should NOT fire a letter."""
    _seed_prequal_and_app("app_partial", "pq_partial")
    vision_payload = '{"doc_type": "bank_stmt", "confidence": 0.9, "extracted_fields": {}, "warnings": []}'
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision", return_value=vision_payload):
        bank_doc = _upload_classified_doc(app_client, auth_headers, stub_s3, "app_partial", "bank_stmt", classify=False)
        resp = app_client.post(f"/api/intake/upload/{bank_doc}/classify", headers=auth_headers)

    body = resp.get_json()
    assert resp.status_code == 200
    assert "autofired_letter" not in body  # nothing fired — only 1 of 3 required docs


def test_classify_skips_autofire_when_recent_letter_exists(app_client, auth_headers, stub_s3):
    """Classifying after a letter slot was just claimed for the app should be deduped."""
    from shared.db import get_conn, insert
    import time
    _seed_prequal_and_app("app_dedup", "pq_dedup")

    # Pre-claim the letter slot just now so the atomic dedup blocks the new fire.
    with get_conn() as conn:
        insert(conn, "letter_claims", {
            "application_id": "app_dedup",
            "claimed_at":     str(time.time()),
        })

    vision_payload = '{"doc_type": "bank_stmt", "confidence": 0.9, "extracted_fields": {}, "warnings": []}'
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.get_default_client", return_value=stub_s3), \
         patch("loan_officer.intake.ocr_classifier.chat_with_vision", return_value=vision_payload):
        _upload_classified_doc(app_client, auth_headers, stub_s3, "app_dedup", "rent_roll")
        _upload_classified_doc(app_client, auth_headers, stub_s3, "app_dedup", "purchase_contract")
        bank_doc = _upload_classified_doc(app_client, auth_headers, stub_s3, "app_dedup", "bank_stmt", classify=False)
        resp = app_client.post(f"/api/intake/upload/{bank_doc}/classify", headers=auth_headers)

    body = resp.get_json()
    assert "autofired_letter" in body
    assert body["autofired_letter"]["skipped"].startswith("letter_already_sent")


def test_completeness_falls_back_to_declared_doc_type(app_client, auth_headers, stub_s3):
    """Unclassified docs still count via declared_doc_type."""
    with patch("loan_officer.intake.upload.get_default_client", return_value=stub_s3):
        p = app_client.post("/api/intake/upload/presign", headers=auth_headers,
                            json={"deal_id": "d", "filename": "r.pdf",
                                  "content_type": "application/pdf",
                                  "application_id": "app_56",
                                  "declared_doc_type": "rent_roll"}).get_json()
        app_client.post("/api/intake/upload/confirm", headers=auth_headers,
                        json={"doc_id": p["doc_id"]})
        resp = app_client.get("/api/intake/applications/app_56/completeness?product=dscr",
                              headers=auth_headers)
    body = resp.get_json()
    assert "rent_roll" in body["received"]
