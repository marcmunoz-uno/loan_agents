"""
loan_officer/tests/test_intake_upload.py — upload.py utility tests.
No network: the S3Client is the stubbed one from conftest.
"""

from __future__ import annotations

import pytest

from shared.s3_client import S3Client, S3NotConfigured
from loan_officer.intake.upload import (
    UploadError,
    presign_upload,
    confirm_upload,
    get_upload_status,
    list_docs_for_deal,
    list_docs_for_application,
    attach_to_application,
)


def test_presign_creates_row_and_returns_url(temp_db, stub_s3):
    out = presign_upload(
        deal_id="deal_42",
        filename="bank-stmt oct.pdf",
        content_type="application/pdf",
        user_id="usr_marc",
        declared_doc_type="bank_stmt",
        s3=stub_s3,
    )
    assert out["doc_id"].startswith("doc_test_")
    assert out["upload_url"].startswith("https://")
    assert out["status"] == "presigned"

    row = get_upload_status(out["doc_id"])
    assert row is not None
    assert row["status"] == "presigned"
    assert row["deal_id"] == "deal_42"
    assert row["filename"] == "bank-stmt oct.pdf"
    assert row["declared_doc_type"] == "bank_stmt"


def test_presign_rejects_empty_filename(temp_db, stub_s3):
    with pytest.raises(UploadError):
        presign_upload(deal_id="d", filename="", content_type="application/pdf", s3=stub_s3)


def test_presign_rejects_empty_content_type(temp_db, stub_s3):
    with pytest.raises(UploadError):
        presign_upload(deal_id="d", filename="x.pdf", content_type="", s3=stub_s3)


def test_presign_raises_when_s3_unconfigured(temp_db):
    bare = S3Client(bucket="")
    with pytest.raises(S3NotConfigured):
        presign_upload(deal_id="d", filename="x.pdf", content_type="application/pdf", s3=bare)


def test_confirm_flips_status_and_records_size(temp_db, stub_s3):
    p = presign_upload(deal_id="d", filename="x.pdf", content_type="application/pdf", s3=stub_s3)
    row = confirm_upload(doc_id=p["doc_id"], s3=stub_s3)
    assert row["status"] == "uploaded"
    assert row["size_bytes"] == 12345
    assert row["uploaded_at"]


def test_confirm_unknown_doc_raises(temp_db):
    with pytest.raises(UploadError):
        confirm_upload(doc_id="doc_does_not_exist", verify_in_s3=False)


def test_confirm_is_idempotent_on_uploaded(temp_db, stub_s3):
    p = presign_upload(deal_id="d", filename="x.pdf", content_type="application/pdf", s3=stub_s3)
    confirm_upload(doc_id=p["doc_id"], s3=stub_s3)
    again = confirm_upload(doc_id=p["doc_id"], s3=stub_s3)
    assert again["status"] == "uploaded"


def test_confirm_marks_failed_when_object_missing(temp_db, stub_s3):
    p = presign_upload(deal_id="d", filename="x.pdf", content_type="application/pdf", s3=stub_s3)
    stub_s3.head_object.side_effect = RuntimeError("404 NoSuchKey")
    with pytest.raises(UploadError):
        confirm_upload(doc_id=p["doc_id"], s3=stub_s3)
    row = get_upload_status(p["doc_id"])
    assert row["status"] == "failed"
    assert "S3 head failed" in row["error_message"]


def test_list_docs_for_deal_orders_newest_first(temp_db, stub_s3):
    # Two presigns under the same deal — list returns both
    from unittest.mock import MagicMock
    from shared.s3_client import PresignedUpload
    stub_s3.generate_presigned_put = MagicMock(side_effect=[
        PresignedUpload(doc_id="doc_a", bucket="b", s3_key="k/a", upload_url="u/a", expires_in=900),
        PresignedUpload(doc_id="doc_b", bucket="b", s3_key="k/b", upload_url="u/b", expires_in=900),
    ])
    presign_upload(deal_id="d", filename="a.pdf", content_type="application/pdf", s3=stub_s3)
    presign_upload(deal_id="d", filename="b.pdf", content_type="application/pdf", s3=stub_s3)
    docs = list_docs_for_deal("d")
    assert {d["doc_id"] for d in docs} == {"doc_a", "doc_b"}


def test_attach_to_application_sets_app_id(temp_db, stub_s3):
    p = presign_upload(deal_id="d", filename="x.pdf", content_type="application/pdf", s3=stub_s3)
    out = attach_to_application(p["doc_id"], "app_999")
    assert out["application_id"] == "app_999"
    apps = list_docs_for_application("app_999")
    assert len(apps) == 1


def test_attach_to_application_unknown_returns_none(temp_db):
    assert attach_to_application("doc_missing", "app_x") is None
