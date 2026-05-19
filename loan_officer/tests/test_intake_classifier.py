"""
loan_officer/tests/test_intake_classifier.py — DocumentClassifier tests.
Mocks shared.llm.chat_with_vision + the S3 stub. No network.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from loan_officer.intake.upload import presign_upload, confirm_upload
from loan_officer.intake.ocr_classifier import (
    DOC_TYPES,
    ClassifierError,
    DocumentClassifier,
)


def _upload(stub_s3, content_type: str = "application/pdf"):
    p = presign_upload(deal_id="d", filename="x.pdf", content_type=content_type, s3=stub_s3)
    confirm_upload(doc_id=p["doc_id"], s3=stub_s3)
    return p["doc_id"]


def test_classify_happy_path_persists_result(temp_db, stub_s3):
    doc_id = _upload(stub_s3)
    vision_payload = json.dumps({
        "doc_type": "bank_stmt",
        "confidence": 0.92,
        "extracted_fields": {"bank_name": "Chase", "ending_balance": 8500},
        "warnings": [],
    })
    with patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value=vision_payload) as vc:
        result = DocumentClassifier(s3=stub_s3).classify(doc_id)

    assert result.doc_type == "bank_stmt"
    assert result.confidence == 0.92
    assert result.extracted_fields["bank_name"] == "Chase"

    # Vision was called with the doc bytes
    call_kwargs = vc.call_args.kwargs
    assert call_kwargs["media"][0]["media_type"] == "application/pdf"
    assert call_kwargs["media"][0]["data"] == b"fake-pdf-bytes"

    # Row was persisted as classified
    from loan_officer.intake.upload import get_upload_status
    row = get_upload_status(doc_id)
    assert row["status"] == "classified"
    assert row["classified_doc_type"] == "bank_stmt"
    assert row["confidence"] == 0.92
    assert row["classified_at"]


def test_classify_unknown_doc_raises(temp_db, stub_s3):
    with pytest.raises(ClassifierError):
        DocumentClassifier(s3=stub_s3).classify("doc_missing")


def test_classify_not_uploaded_raises(temp_db, stub_s3):
    p = presign_upload(deal_id="d", filename="x.pdf", content_type="application/pdf", s3=stub_s3)
    with pytest.raises(ClassifierError, match="not ready"):
        DocumentClassifier(s3=stub_s3).classify(p["doc_id"])


def test_classify_unsupported_media_marks_failed(temp_db, stub_s3):
    doc_id = _upload(stub_s3, content_type="application/zip")
    with pytest.raises(ClassifierError, match="unsupported"):
        DocumentClassifier(s3=stub_s3).classify(doc_id)

    from loan_officer.intake.upload import get_upload_status
    row = get_upload_status(doc_id)
    assert row["status"] == "failed"


def test_classify_vision_failure_marks_failed(temp_db, stub_s3):
    doc_id = _upload(stub_s3)
    with patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               side_effect=RuntimeError("API down")):
        with pytest.raises(RuntimeError, match="API down"):
            DocumentClassifier(s3=stub_s3).classify(doc_id)

    from loan_officer.intake.upload import get_upload_status
    row = get_upload_status(doc_id)
    assert row["status"] == "failed"
    assert "vision call failed" in row["error_message"]


def test_parse_response_handles_fenced_json(temp_db, stub_s3):
    cls = DocumentClassifier(s3=stub_s3)
    raw = '```json\n{"doc_type": "w2", "confidence": 0.8, "extracted_fields": {}, "warnings": []}\n```'
    res = cls._parse_response("doc_x", raw)
    assert res.doc_type == "w2"
    assert res.confidence == 0.8


def test_parse_response_handles_trailing_prose(temp_db, stub_s3):
    cls = DocumentClassifier(s3=stub_s3)
    raw = 'Looking at this, I see:\n{"doc_type": "tax_return", "confidence": 0.7, "extracted_fields": {"tax_year": 2023}, "warnings": []}\nThe tax year is 2023.'
    res = cls._parse_response("doc_x", raw)
    assert res.doc_type == "tax_return"
    assert res.extracted_fields == {"tax_year": 2023}


def test_parse_response_invalid_doc_type_falls_back_to_other(temp_db, stub_s3):
    cls = DocumentClassifier(s3=stub_s3)
    raw = '{"doc_type": "made_up_thing", "confidence": 0.9, "extracted_fields": {}, "warnings": []}'
    res = cls._parse_response("doc_x", raw)
    assert res.doc_type == "other"


def test_parse_response_clamps_confidence(temp_db, stub_s3):
    cls = DocumentClassifier(s3=stub_s3)
    raw = '{"doc_type": "w2", "confidence": 1.7, "extracted_fields": {}, "warnings": []}'
    assert cls._parse_response("d", raw).confidence == 1.0

    raw = '{"doc_type": "w2", "confidence": -0.4, "extracted_fields": {}, "warnings": []}'
    assert cls._parse_response("d", raw).confidence == 0.0


def test_parse_response_unparseable_falls_back_with_warning(temp_db, stub_s3):
    cls = DocumentClassifier(s3=stub_s3)
    res = cls._parse_response("d", "this is not JSON at all")
    assert res.doc_type == "other"
    assert res.warnings
    assert "not valid JSON" in res.warnings[0]


def test_classify_batch_handles_partial_failures(temp_db, stub_s3):
    doc_a = _upload(stub_s3)
    # Second doc is never uploaded → will fail classification with ClassifierError
    p = presign_upload(deal_id="d", filename="b.pdf", content_type="application/pdf", s3=stub_s3)
    doc_b = p["doc_id"]

    vision_payload = '{"doc_type": "w2", "confidence": 0.9, "extracted_fields": {}, "warnings": []}'
    with patch("loan_officer.intake.ocr_classifier.chat_with_vision",
               return_value=vision_payload):
        results = DocumentClassifier(s3=stub_s3).classify_batch([doc_a, doc_b])

    assert results[0].doc_type == "w2"
    assert results[1].doc_type == "other"
    assert any("classify failed" in w for w in results[1].warnings)


def test_doc_types_unique_and_have_other_fallback():
    assert len(DOC_TYPES) == len(set(DOC_TYPES))
    assert "other" in DOC_TYPES
