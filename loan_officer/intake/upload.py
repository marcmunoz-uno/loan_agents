"""
loan_officer/intake/upload.py — Flask blueprint for document uploads with signed-URL flow.
"""

from __future__ import annotations

from flask import Blueprint, request, jsonify

from shared.auth import require_tranchi_auth

# TODO: import shared.storage.s3_client once implemented

intake_upload_bp = Blueprint("intake_upload", __name__, url_prefix="/api/loan/intake")


# ── POST /api/loan/intake/upload/presign ─────────────────────────────────────


@intake_upload_bp.route("/upload/presign", methods=["POST"])
@require_tranchi_auth
def presign_upload():
    """
    Return a pre-signed S3 PUT URL for a single document.

    Body:
        {
            "deal_id": "deal_abc123",
            "filename": "bank_statement_oct.pdf",
            "content_type": "application/pdf"
        }

    Returns:
        {
            "upload_url": "<signed S3 PUT URL>",
            "doc_id": "<generated doc_id>",
            "s3_key": "deals/{deal_id}/docs/{doc_id}/{filename}",
            "expires_in": 900
        }

    TODO: call s3_client.generate_presigned_put(deal_id, filename, content_type)
    TODO: persist a pending doc record in loan_documents table
    """
    # TODO: validate body, generate doc_id, call s3_client
    raise NotImplementedError("TODO: implement presign_upload")


# ── POST /api/loan/intake/upload/confirm ─────────────────────────────────────


@intake_upload_bp.route("/upload/confirm", methods=["POST"])
@require_tranchi_auth
def confirm_upload():
    """
    Mark a document upload as complete after the client finishes the S3 PUT.

    Body:
        {
            "doc_id": "doc_xyz",
            "deal_id": "deal_abc123"
        }

    Returns: {"doc_id": ..., "status": "received", "queued_for_ocr": true}

    TODO: update loan_documents row status to "uploaded"
    TODO: enqueue doc_id for ocr_classifier processing
    """
    # TODO: fetch pending doc record, flip status, trigger OCR job
    raise NotImplementedError("TODO: implement confirm_upload")


# ── GET /api/loan/intake/upload/<doc_id> ──────────────────────────────────────


@intake_upload_bp.route("/upload/<doc_id>", methods=["GET"])
@require_tranchi_auth
def get_upload_status(doc_id: str):
    """
    Return upload + classification status for a single document.

    Returns: {"doc_id": ..., "status": "uploaded"|"classifying"|"classified"|"failed",
              "doc_type": <classified type or null>, "s3_key": ...}

    TODO: query loan_documents by doc_id and return status row
    """
    # TODO: fetchone by doc_id, return status dict
    raise NotImplementedError("TODO: implement get_upload_status")
