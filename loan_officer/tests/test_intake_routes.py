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
