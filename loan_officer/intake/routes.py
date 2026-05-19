"""
loan_officer/intake/routes.py — Flask blueprint: /api/intake/*

The intake doc pipeline's HTTP surface. All endpoints require
`Authorization: Bearer <TRANCHI_API_SECRET>`.

Endpoints
---------
POST   /api/intake/upload/presign         — reserve a doc row, return presigned PUT URL
POST   /api/intake/upload/confirm         — borrower's PUT finished; verify in S3
POST   /api/intake/upload/<doc_id>/classify — synchronously OCR a doc (Claude vision)
GET    /api/intake/upload/<doc_id>        — current upload + classification status
GET    /api/intake/deals/<deal_id>/docs   — all intake docs for a borrower deal
GET    /api/intake/applications/<app_id>/docs — all intake docs attached to an app
GET    /api/intake/applications/<app_id>/completeness?product=dscr — doc-checklist gap report
POST   /api/intake/upload/<doc_id>/attach — wire an intake doc to a loan_application
"""

from __future__ import annotations

from flask import Blueprint, request, jsonify

from shared.auth import require_tranchi_auth
from shared.s3_client import S3NotConfigured

from loan_officer.intake.upload import (
    UploadError,
    presign_upload,
    confirm_upload,
    get_upload_status,
    list_docs_for_deal,
    list_docs_for_application,
    attach_to_application,
)
from loan_officer.intake.ocr_classifier import (
    ClassifierError,
    DocumentClassifier,
)
from loan_officer.intake.completeness import check_completeness, gap_message


intake_bp = Blueprint("intake", __name__, url_prefix="/api/intake")


# ── /upload/presign ───────────────────────────────────────────────────────────

@intake_bp.route("/upload/presign", methods=["POST"])
@require_tranchi_auth
def presign():
    body = request.get_json(silent=True) or {}
    try:
        result = presign_upload(
            deal_id=str(body.get("deal_id", "")),
            filename=str(body.get("filename", "")),
            content_type=str(body.get("content_type", "")),
            user_id=str(body.get("user_id", "")),
            declared_doc_type=str(body.get("declared_doc_type", "")),
            application_id=str(body.get("application_id", "")),
        )
    except UploadError as e:
        return jsonify({"error": str(e)}), 400
    except S3NotConfigured as e:
        return jsonify({"error": str(e)}), 503
    return jsonify(result)


# ── /upload/confirm ───────────────────────────────────────────────────────────

@intake_bp.route("/upload/confirm", methods=["POST"])
@require_tranchi_auth
def confirm():
    body = request.get_json(silent=True) or {}
    doc_id = str(body.get("doc_id", ""))
    if not doc_id:
        return jsonify({"error": "doc_id is required"}), 400
    try:
        row = confirm_upload(
            doc_id=doc_id,
            size_bytes=int(body.get("size_bytes", 0) or 0),
            verify_in_s3=bool(body.get("verify_in_s3", True)),
        )
    except UploadError as e:
        return jsonify({"error": str(e)}), 404 if "unknown doc_id" in str(e) else 400
    return jsonify(row)


# ── /upload/<doc_id>/classify ─────────────────────────────────────────────────

@intake_bp.route("/upload/<doc_id>/classify", methods=["POST"])
@require_tranchi_auth
def classify(doc_id: str):
    """
    Run the Claude vision classifier on a single uploaded doc, synchronously.
    Returns the ClassificationResult dict (and the row is persisted with status='classified').
    """
    try:
        result = DocumentClassifier().classify(doc_id)
    except ClassifierError as e:
        msg = str(e).lower()
        code = 404 if "unknown doc_id" in msg else 409 if "not ready" in msg else 400
        return jsonify({"error": str(e)}), code
    except S3NotConfigured as e:
        return jsonify({"error": str(e)}), 503
    except RuntimeError as e:
        # Bubble up vision / Anthropic errors as 502 — upstream dependency failed.
        return jsonify({"error": str(e)}), 502
    return jsonify(result.to_dict())


# ── /upload/<doc_id> (status) ─────────────────────────────────────────────────

@intake_bp.route("/upload/<doc_id>", methods=["GET"])
@require_tranchi_auth
def status(doc_id: str):
    row = get_upload_status(doc_id)
    if row is None:
        return jsonify({"error": "doc not found"}), 404
    return jsonify(row)


# ── /upload/<doc_id>/attach ───────────────────────────────────────────────────

@intake_bp.route("/upload/<doc_id>/attach", methods=["POST"])
@require_tranchi_auth
def attach(doc_id: str):
    body = request.get_json(silent=True) or {}
    application_id = str(body.get("application_id", ""))
    if not application_id:
        return jsonify({"error": "application_id is required"}), 400
    row = attach_to_application(doc_id, application_id)
    if row is None:
        return jsonify({"error": "doc not found"}), 404
    return jsonify(row)


# ── /deals/<deal_id>/docs ─────────────────────────────────────────────────────

@intake_bp.route("/deals/<deal_id>/docs", methods=["GET"])
@require_tranchi_auth
def docs_by_deal(deal_id: str):
    return jsonify({"deal_id": deal_id, "docs": list_docs_for_deal(deal_id)})


# ── /applications/<app_id>/docs ──────────────────────────────────────────────

@intake_bp.route("/applications/<app_id>/docs", methods=["GET"])
@require_tranchi_auth
def docs_by_application(app_id: str):
    return jsonify({"application_id": app_id, "docs": list_docs_for_application(app_id)})


# ── /applications/<app_id>/completeness ──────────────────────────────────────

@intake_bp.route("/applications/<app_id>/completeness", methods=["GET"])
@require_tranchi_auth
def completeness(app_id: str):
    """
    Compare uploaded + classified docs against the per-product checklist.
    `product` defaults to dscr (matches completeness._CHECKLISTS).
    """
    product = (request.args.get("product") or "dscr").lower()
    docs = list_docs_for_application(app_id)

    # We prefer the classifier's verdict; fall back to declared_doc_type.
    received = [
        (d.get("classified_doc_type") or d.get("declared_doc_type") or "").strip()
        for d in docs
        if d.get("status") in ("uploaded", "classified")
    ]
    received = [r for r in received if r]

    report = check_completeness(product, received)
    return jsonify({
        "application_id": app_id,
        "product": report.product,
        "received": report.received,
        "required_missing": report.required_missing,
        "optional_missing": report.optional_missing,
        "is_complete": report.is_complete,
        "completion_pct": report.completion_pct,
        "message": gap_message(report),
    })
