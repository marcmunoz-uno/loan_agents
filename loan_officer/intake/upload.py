"""
loan_officer/intake/upload.py — Intake-document upload utilities.

Pure functions over the S3 client + intake_documents table. The Flask layer
lives in loan_officer/intake/routes.py; this module is the implementation
seam, mocked in tests.

Lifecycle:
    presigned → uploaded → classifying → classified | failed

  presign_upload   creates the doc row in 'presigned' state and returns the
                   PUT URL the borrower (or the Tranchi UI) uploads to.
  confirm_upload   borrower's client tells us the PUT succeeded → we flip
                   status to 'uploaded' and (optionally) kick off OCR.
  get_upload_status returns the current row.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db import get_conn, fetchone, insert, update
from shared.s3_client import S3Client, S3NotConfigured, get_default_client


# ── Errors ────────────────────────────────────────────────────────────────────

class UploadError(RuntimeError):
    """Caller-recoverable upload error (bad input, missing doc, etc.)."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: Optional[dict]) -> Optional[dict]:
    """Decode the JSON blob columns we store as strings."""
    if row is None:
        return None
    out = dict(row)
    for key in ("extracted_fields", "warnings"):
        raw = out.get(key)
        if isinstance(raw, str):
            try:
                out[key] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                out[key] = {} if key == "extracted_fields" else []
    return out


# ── Public API ────────────────────────────────────────────────────────────────

def presign_upload(
    *,
    deal_id: str,
    filename: str,
    content_type: str,
    user_id: str = "",
    declared_doc_type: str = "",
    application_id: str = "",
    s3: Optional[S3Client] = None,
) -> dict[str, Any]:
    """
    Reserve a doc row and return a presigned PUT URL.

    Returns:
        {
            "doc_id": "doc_xyz",
            "upload_url": "<S3 PUT URL>",
            "s3_key": "intake/deals/{deal_id}/docs/doc_xyz/{filename}",
            "bucket": "...",
            "expires_in": 900,
            "status": "presigned"
        }

    Raises:
        UploadError    if filename or content_type is empty
        S3NotConfigured if AWS_S3_BUCKET is not set
    """
    if not filename:
        raise UploadError("filename is required")
    if not content_type:
        raise UploadError("content_type is required")

    s3 = s3 or get_default_client()
    if not s3.configured:
        raise S3NotConfigured("AWS_S3_BUCKET is not set — cannot presign uploads")

    presigned = s3.generate_presigned_put(
        deal_id=deal_id, filename=filename, content_type=content_type,
    )

    now = _now()
    with get_conn() as conn:
        insert(conn, "intake_documents", {
            "doc_id": presigned.doc_id,
            "deal_id": deal_id,
            "application_id": application_id,
            "user_id": user_id,
            "filename": filename,
            "content_type": content_type,
            "s3_bucket": presigned.bucket,
            "s3_key": presigned.s3_key,
            "declared_doc_type": declared_doc_type,
            "status": "presigned",
            "created_at": now,
            "updated_at": now,
        })

    return {
        "doc_id": presigned.doc_id,
        "upload_url": presigned.upload_url,
        "s3_key": presigned.s3_key,
        "bucket": presigned.bucket,
        "expires_in": presigned.expires_in,
        "status": "presigned",
    }


def confirm_upload(
    *,
    doc_id: str,
    size_bytes: int = 0,
    s3: Optional[S3Client] = None,
    verify_in_s3: bool = True,
) -> dict[str, Any]:
    """
    Mark a doc as uploaded. Optionally HEAD the object first to verify it landed.

    Returns the updated row as a dict.
    """
    row = _fetch_row(doc_id)
    if row is None:
        raise UploadError(f"unknown doc_id: {doc_id!r}")
    if row["status"] not in ("presigned", "uploaded", "failed"):
        # idempotent — but don't clobber a classified row
        return row

    actual_size = size_bytes
    if verify_in_s3:
        client = s3 or get_default_client()
        if client.configured:
            try:
                head = client.head_object(row["s3_key"])
                actual_size = int(head.get("ContentLength", actual_size) or 0)
            except Exception as e:
                _set_failure(doc_id, f"S3 head failed: {e}")
                raise UploadError(f"object not found in S3 for {doc_id}: {e}")

    now = _now()
    with get_conn() as conn:
        update(conn, "intake_documents", {
            "status": "uploaded",
            "size_bytes": actual_size,
            "uploaded_at": now,
            "updated_at": now,
        }, "doc_id = ?", (doc_id,))

    return _fetch_row(doc_id) or {}


def get_upload_status(doc_id: str) -> Optional[dict[str, Any]]:
    """Read the current state of a doc, or None if it doesn't exist."""
    return _fetch_row(doc_id)


def list_docs_for_deal(deal_id: str) -> list[dict[str, Any]]:
    """Return every intake doc tied to a deal_id, newest-first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_documents WHERE deal_id = ? ORDER BY created_at DESC",
            (deal_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[return-value]


def list_docs_for_application(application_id: str) -> list[dict[str, Any]]:
    """Return every intake doc tied to a loan application."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM intake_documents WHERE application_id = ? ORDER BY created_at DESC",
            (application_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[return-value]


def attach_to_application(doc_id: str, application_id: str) -> Optional[dict[str, Any]]:
    """Wire a previously intake-only doc to an actual loan_applications row."""
    with get_conn() as conn:
        update(conn, "intake_documents", {
            "application_id": application_id,
            "updated_at": _now(),
        }, "doc_id = ?", (doc_id,))
    return _fetch_row(doc_id)


# ── Internals ─────────────────────────────────────────────────────────────────

def _fetch_row(doc_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = fetchone(conn, "SELECT * FROM intake_documents WHERE doc_id = ?", (doc_id,))
    return _row_to_dict(row)


def _set_failure(doc_id: str, message: str) -> None:
    with get_conn() as conn:
        update(conn, "intake_documents", {
            "status": "failed",
            "error_message": message[:500],
            "updated_at": _now(),
        }, "doc_id = ?", (doc_id,))
