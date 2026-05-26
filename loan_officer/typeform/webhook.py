"""
loan_officer/typeform/webhook.py — Flask blueprint mounted at /api/loan/webhook.

Endpoint:
    POST /api/loan/webhook/typeform-submit

Typeform signs the raw request body with HMAC SHA256 + base64 and sends it in
the `Typeform-Signature` header as `sha256=<base64>`. See
https://www.typeform.com/developers/webhooks/secure-your-webhooks/.

Flow per submission:
    1. Verify Typeform-Signature against TYPEFORM_WEBHOOK_SECRET
    2. Parse form_response, map → intake row
    3. Soft-prequal score
    4. Persist loan_borrower_intakes row (idempotent on token)
    5. Compose + send AI Loan Officer email
    6. Fire borrower_intake_created outbound zap (best-effort)
    7. Return 200 with intake_id + soft prequal summary
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from shared.db import fetchone, get_conn, insert, update
from loan_officer.arive_zapier import fire_zap
from loan_officer.typeform.mapper import map_payload
from loan_officer.typeform.soft_prequal import score as soft_prequal_score
from loan_officer.typeform.email_composer import compose_email, send_email
from loan_officer.typeform.letter_autofire import fire_letter_async

logger = logging.getLogger(__name__)

typeform_webhook_bp = Blueprint(
    "typeform_webhook", __name__, url_prefix="/api/loan/webhook"
)


# ── Signature verification ───────────────────────────────────────────────────

def _verify_typeform_signature(raw_body: bytes, header_value: str, secret: str) -> bool:
    """
    Typeform sends `Typeform-Signature: sha256=<base64-encoded-digest>`.
    """
    if not header_value or not header_value.startswith("sha256="):
        return False
    received_b64 = header_value.split("=", 1)[1]
    try:
        received = base64.b64decode(received_b64)
    except Exception:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, received)


# ── Endpoint ─────────────────────────────────────────────────────────────────

@typeform_webhook_bp.route("/typeform-submit", methods=["POST"])
def typeform_submit():
    raw_body = request.get_data()
    secret = os.environ.get("TYPEFORM_WEBHOOK_SECRET", "")
    sig = request.headers.get("Typeform-Signature", "")

    if secret:
        if not _verify_typeform_signature(raw_body, sig, secret):
            logger.warning("[typeform_webhook] invalid signature (header=%r)", sig[:40])
            return jsonify({"error": "Invalid Typeform signature"}), 401
    else:
        logger.warning("[typeform_webhook] TYPEFORM_WEBHOOK_SECRET not set — skipping signature check")

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON body"}), 400

    form_response = payload.get("form_response") or {}
    if not form_response:
        return jsonify({"error": "Missing form_response"}), 400

    intake = map_payload(form_response)
    token = intake.get("typeform_response_id", "")
    if not token:
        return jsonify({"error": "Missing form_response.token"}), 400

    # Idempotency — Typeform retries on non-2xx.
    with get_conn() as conn:
        existing = fetchone(
            conn,
            "SELECT intake_id, soft_prequal_status, soft_prequal_score, email_send_status "
            "FROM loan_borrower_intakes WHERE typeform_response_id = ?",
            (token,),
        )
        if existing:
            logger.info("[typeform_webhook] duplicate token %s — returning prior result", token)
            return jsonify({
                "ok": True,
                "duplicate": True,
                **existing,
            }), 200

        prequal = soft_prequal_score(intake)
        prequal_dict = dataclasses.asdict(prequal)

        intake_id = f"bi_{uuid.uuid4().hex[:12]}"
        row = {
            **intake,
            "intake_id": intake_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "soft_prequal_status": prequal.status,
            "soft_prequal_score":  prequal.score,
            "missing_required_docs": json.dumps(prequal.missing_required_docs),
            "decision_reasons":      json.dumps(prequal.decision_reasons),
            "email_send_status":  "pending",
            "raw_payload":        json.dumps(payload),
        }
        insert(conn, "loan_borrower_intakes", row)

    # ── Compose + send AI Loan Officer email ─────────────────────────────────
    # Two paths, picked at the row level:
    #
    #   Letter path (preferred): if the borrower uploaded any asset statement
    #   URL via Typeform, spawn a daemon thread that OCRs the statements,
    #   creates a `loan_prequals` row from the intake data, and fires the
    #   prequal-letter pipeline (PDF + email via Zapier MCP Gmail). The
    #   thread updates this intake row when it finishes. We mark the row
    #   `letter_pending` here so the webhook can return 200 inside
    #   Typeform's 10s timeout window.
    #
    #   Soft-email fallback: if no statements were uploaded, send the
    #   existing intake email so the borrower still gets a same-day reply.
    has_statements = any(
        (row.get(f) or "").strip()
        for f in (
            "asset_statement_recent_url",
            "asset_statement_previous_url",
            "asset_statement_extra_url",
        )
    )

    if has_statements and row.get("email"):
        with get_conn() as conn:
            update(
                conn,
                "loan_borrower_intakes",
                {"email_send_status": "letter_pending"},
                "intake_id = ?",
                (intake_id,),
            )
        fire_letter_async(intake_id, row)
        email_status = "letter_pending"
        email = {"subject": "", "body": ""}
    else:
        try:
            email = compose_email(row, prequal_dict)
        except Exception as e:
            logger.exception("[typeform_webhook] compose_email failed")
            email = {"subject": "", "body": ""}
            send_result = {"ok": False, "error": f"compose failed: {e}"}
        else:
            send_result = send_email(row.get("email", ""), email["subject"], email["body"], correlation_id=intake_id)

        email_status = "sent" if send_result.get("ok") else ("skipped" if send_result.get("skipped") else "failed")
        with get_conn() as conn:
            update(
                conn,
                "loan_borrower_intakes",
                {
                    "email_subject":     email.get("subject", ""),
                    "email_body":        email.get("body", ""),
                    "email_send_status": email_status,
                    "email_sent_at":     send_result.get("sent_at", "") if email_status == "sent" else "",
                    "email_error":       send_result.get("error", "") if email_status != "sent" else "",
                },
                "intake_id = ?",
                (intake_id,),
            )

    # ── Outbound notification ─────────────────────────────────────────────────
    fire_zap(
        "borrower_intake_created",
        {
            "intake_id": intake_id,
            "email": row.get("email", ""),
            "first_name": row.get("first_name", ""),
            "last_name":  row.get("last_name", ""),
            "phone": row.get("phone", ""),
            "soft_prequal_status": prequal.status,
            "soft_prequal_score":  prequal.score,
            "spoke_to_loan_officer": row.get("spoke_to_loan_officer", ""),
        },
        correlation_id=intake_id,
    )

    return jsonify({
        "ok": True,
        "intake_id": intake_id,
        "soft_prequal_status": prequal.status,
        "soft_prequal_score":  prequal.score,
        "missing_required_docs": prequal.missing_required_docs,
        "email_send_status": email_status,
    }), 200
