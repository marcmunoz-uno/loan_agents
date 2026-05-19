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

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from flask import Blueprint, request, jsonify

from shared.db import get_conn, fetchone

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

    Side effect: if this classification flips completeness to is_complete=True
    for the doc's application and no letter has been sent in the last 24h,
    auto-generates + sends the pre-qualification letter. The response includes
    an `autofired_letter` block when that happens.
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

    response: dict[str, Any] = result.to_dict()
    autofired = _maybe_autofire_prequal_letter(doc_id)
    if autofired:
        response["autofired_letter"] = autofired
    return jsonify(response)


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

# ── Auto-trigger helpers ──────────────────────────────────────────────────────

_LETTER_DEDUP_SECONDS = 24 * 60 * 60  # 24h


def _maybe_autofire_prequal_letter(doc_id: str) -> Optional[dict[str, Any]]:
    """
    After a successful classify(), check whether the doc's application is now
    complete and no letter has been sent in the last 24h. If both: generate +
    auto-send and return a summary block. Otherwise None.

    Returns:
        {"letter_id", "max_pp_low", "max_pp_high", "mcp_send_status",
         "zap_fired", "pdf_url"}  on fire
        {"skipped": "<reason>"}                                          on skip
        None                                                              if nothing applicable
    """
    # 1. Find the application + prequal this doc belongs to
    doc = get_upload_status(doc_id)
    if not doc or not doc.get("application_id"):
        return None
    app_id = doc["application_id"]

    with get_conn() as conn:
        app_row = fetchone(
            conn,
            "SELECT id, prequal_id FROM loan_applications WHERE id = ?",
            (app_id,),
        )
    if not app_row or not app_row.get("prequal_id"):
        return None
    prequal_id = app_row["prequal_id"]

    # 2. Resolve the product the borrower is pursuing
    with get_conn() as conn:
        pq_row = fetchone(
            conn,
            "SELECT suggested_product FROM loan_prequals WHERE id = ?",
            (prequal_id,),
        )
    product = (pq_row.get("suggested_product") if pq_row else "dscr") or "dscr"

    # 3. Run completeness against current intake state
    docs = list_docs_for_application(app_id)
    received = [
        (d.get("classified_doc_type") or d.get("declared_doc_type") or "").strip()
        for d in docs
        if d.get("status") in ("uploaded", "classified")
    ]
    received = [r for r in received if r]
    report = check_completeness(product, received)
    if not report.is_complete:
        return None

    # 4. Dedup — skip if a letter was sent in the last 24h for this app
    from loan_officer.prequal_letter import (
        latest_letter_for_application,
        generate_and_send,
    )

    last = latest_letter_for_application(app_id)
    if last:
        try:
            issued = datetime.fromisoformat(last["issued_at"])
        except (KeyError, ValueError, TypeError):
            issued = None
        if issued and (datetime.now(timezone.utc) - issued) < timedelta(seconds=_LETTER_DEDUP_SECONDS):
            return {"skipped": f"letter_already_sent:{last['letter_id']}"}

    # 5. Fire
    try:
        letter = generate_and_send(prequal_id)
    except Exception as e:
        return {"skipped": f"generate_failed:{e}"}

    return {
        "letter_id":        letter.letter_id,
        "max_pp_low":       letter.max_pp_low,
        "max_pp_high":      letter.max_pp_high,
        "mcp_send_status":  letter.mcp_send_status,
        "zap_fired":        letter.zap_fired,
        "pdf_url":          letter.pdf_url,
    }


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
