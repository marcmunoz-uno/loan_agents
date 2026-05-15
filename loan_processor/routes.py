"""
loan_processor/routes.py — Flask blueprint: /api/processor/*

All endpoints (except /api/processor/fire-zap/*) require Bearer auth.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, request, jsonify

from shared.auth import require_tranchi_auth
from shared.db import get_conn, insert, update, fetchone
from shared.llm import chat

from loan_processor.pre_underwriting import pre_underwrite, PreUnderwritingReport
from loan_processor.guideline_engine import get_engine

logger = logging.getLogger(__name__)

processor_bp = Blueprint("processor", __name__, url_prefix="/api/processor")

_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()

_now = lambda: datetime.now(timezone.utc).isoformat()


def _report_to_dict(report: PreUnderwritingReport) -> dict:
    """Convert report dataclass to JSON-serializable dict."""
    return {
        "application_id": report.application_id,
        "summary": report.summary,
        "overall_status": report.overall_status,
        "lender_fit": report.lender_fit,
        "conditions": [
            {
                "condition_type": c.condition_type,
                "severity": c.severity,
                "description": c.description,
                "lender_specific": c.lender_specific,
                "required": c.required,
            }
            for c in report.conditions
        ],
        "red_flags": [
            {
                "flag_type": f.flag_type,
                "severity": f.severity,
                "description": f.description,
                "mitigation_suggestion": f.mitigation_suggestion,
            }
            for f in report.red_flags
        ],
        "computed_metrics": report.computed_metrics,
        "suggested_lender": report.suggested_lender,
        "credit_memo_draft": report.credit_memo_draft,
        "generated_at": report.generated_at.isoformat(),
    }


def _save_report(report: PreUnderwritingReport) -> None:
    """Persist the report to the pre_underwriting_reports table."""
    now = _now()
    d = _report_to_dict(report)
    with get_conn() as conn:
        # Upsert by application_id: delete old, insert new
        conn.execute(
            "DELETE FROM pre_underwriting_reports WHERE application_id = ?",
            (report.application_id,)
        )
        conn.commit()
        insert(conn, "pre_underwriting_reports", {
            "application_id": report.application_id,
            "status": report.overall_status,
            "summary": report.summary,
            "overall_status": report.overall_status,
            "lender_fit": json.dumps(report.lender_fit),
            "conditions": json.dumps(d["conditions"]),
            "red_flags": json.dumps(d["red_flags"]),
            "computed_metrics": json.dumps(report.computed_metrics),
            "suggested_lender": report.suggested_lender,
            "credit_memo": report.credit_memo_draft,
            "generated_at": report.generated_at.isoformat(),
        })


# ── POST /api/processor/pre-underwrite/<application_id> ──────────────────────

@processor_bp.route("/pre-underwrite/<application_id>", methods=["POST"])
@require_tranchi_auth
def run_pre_underwrite(application_id: str):
    """
    Run pre-underwriting on an application. Saves and returns the full report.

    If overall_status is 'clean', automatically fires the Arive/Zapier
    ready_for_underwriting event with the credit memo attached.
    """
    try:
        report = pre_underwrite(application_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        logger.error("[processor] pre_underwrite failed: %s", exc, exc_info=True)
        return jsonify({"error": "Pre-underwriting engine error", "detail": str(exc)}), 500

    # Save to DB
    try:
        _save_report(report)
    except Exception as exc:
        logger.warning("[processor] Failed to save report: %s", exc)

    report_dict = _report_to_dict(report)

    # Auto-fire Zapier event when clean
    if report.overall_status == "clean":
        try:
            from loan_officer.arive_zapier import fire_zap
            fire_zap(
                "ready_for_underwriting",
                {
                    "application_id": application_id,
                    "suggested_lender": report.suggested_lender,
                    "credit_memo": report.credit_memo_draft[:2000],   # truncate for webhook payload
                    "conditions_count": len(report.conditions),
                    "top_conditions": [c.description for c in report.conditions[:5]],
                    "computed_metrics": report.computed_metrics,
                },
                correlation_id=application_id,
            )
        except Exception as exc:
            logger.warning("[processor] Zapier fire failed (continuing): %s", exc)

    return jsonify(report_dict), 200


# ── GET /api/processor/pre-underwrite/<application_id> ───────────────────────

@processor_bp.route("/pre-underwrite/<application_id>", methods=["GET"])
@require_tranchi_auth
def get_pre_underwrite(application_id: str):
    """Fetch the latest pre-underwriting report for an application."""
    with get_conn() as conn:
        row = fetchone(
            conn,
            "SELECT * FROM pre_underwriting_reports WHERE application_id = ? ORDER BY generated_at DESC LIMIT 1",
            (application_id,)
        )
    if not row:
        return jsonify({"error": f"No pre-underwriting report found for {application_id}"}), 404

    # Decode JSON fields
    for field_name in ("lender_fit", "conditions", "red_flags", "computed_metrics"):
        if row.get(field_name):
            try:
                row[field_name] = json.loads(row[field_name])
            except Exception:
                pass

    return jsonify(row)


# ── GET /api/processor/guidelines ────────────────────────────────────────────

@processor_bp.route("/guidelines", methods=["GET"])
@require_tranchi_auth
def list_guidelines():
    """Return the full guidelines_index.json."""
    engine = get_engine()
    return jsonify(engine.get_index())


# ── GET /api/processor/guidelines/<lender_id> ────────────────────────────────

@processor_bp.route("/guidelines/<lender_id>", methods=["GET"])
@require_tranchi_auth
def get_guideline(lender_id: str):
    """Return the full markdown guidelines for one lender."""
    engine = get_engine()
    doc = engine.get_guideline_doc(lender_id)
    if doc is None:
        return jsonify({"error": f"Lender '{lender_id}' not found or guideline file missing"}), 404
    entry = engine.get_index().get(lender_id, {})
    return jsonify({
        "lender_id": lender_id,
        "lender": entry.get("lender", ""),
        "product": entry.get("product", ""),
        "content": doc,
        "index_entry": entry,
    })


# ── POST /api/processor/guidelines/check ─────────────────────────────────────

@processor_bp.route("/guidelines/check", methods=["POST"])
@require_tranchi_auth
def check_guidelines():
    """
    Quick lender match without running full pre-UW.

    Body: {"borrower_data": {...}, "property_data": {...}}
    Returns: ranked list of lenders that match.
    """
    body = request.get_json(force=True) or {}
    b = body.get("borrower_data", {})
    p = body.get("property_data", {})

    fico = int(b.get("credit_score") or 0)
    down_pct = float(b.get("down_payment_pct") or 25)
    purchase_price = float(p.get("purchase_price") or 0)
    desired_loan = float(b.get("desired_loan_amount") or 0)
    monthly_rent = float(p.get("monthly_rent") or 0)
    annual_taxes = float(p.get("annual_taxes") or 0)
    annual_insurance = float(p.get("annual_insurance") or 0)

    loan_amount = desired_loan or (purchase_price * (1 - down_pct / 100) if purchase_price else 0)
    ltv = (loan_amount / purchase_price) if purchase_price > 0 else 0.75

    # Rough DSCR estimate
    dscr = None
    if monthly_rent > 0 and loan_amount > 0:
        # Rough 8% rate, 30yr
        r = 0.08 / 12
        n = 360
        monthly_pni = loan_amount * (r * (1 + r) ** n) / ((1 + r) ** n - 1)
        monthly_piti = monthly_pni + annual_taxes / 12 + annual_insurance / 12
        if monthly_piti > 0:
            dscr = round(monthly_rent / monthly_piti, 3)

    # Detect product type from body or infer
    product_type = body.get("product_type", "")
    if not product_type:
        rehab_budget = float(p.get("rehab_budget") or 0)
        if rehab_budget > 5000:
            product_type = "fix_flip"
        elif monthly_rent > 0:
            product_type = "dscr"
        else:
            product_type = "dscr"

    loan_purpose = b.get("loan_purpose", "purchase")
    address = p.get("address", "")
    # Extract state from address
    from loan_processor.pre_underwriting import _extract_state
    state = _extract_state(address)

    engine = get_engine()
    matches = engine.match_lenders(
        fico=fico,
        ltv=ltv,
        product_type=product_type,
        dscr=dscr,
        state=state,
        loan_amount=loan_amount,
        property_type=p.get("property_type", "single_family"),
        loan_purpose=loan_purpose,
    )

    return jsonify({
        "product_type": product_type,
        "computed": {
            "fico": fico,
            "ltv": round(ltv, 4),
            "dscr": dscr,
            "loan_amount": round(loan_amount, 2),
        },
        "lenders": matches[:5],   # top 5
    })


# ── POST /api/processor/chat ──────────────────────────────────────────────────

@processor_bp.route("/chat", methods=["POST"])
@require_tranchi_auth
def processor_chat():
    """
    Tranchi - Loan Processor conversational interface. MLO or borrower asks a
    guideline question and gets specifics from the guidelines back.

    Body: {"user_id": "...", "message": "...", "lender_id": "...", "context": {...}}
    """
    body = request.get_json(force=True) or {}
    user_id = body.get("user_id", "anon")
    message = body.get("message", "")
    lender_id = body.get("lender_id", "")
    context = body.get("context", {})

    if not message:
        return jsonify({"error": "message required"}), 400

    # Optionally inject lender guidelines into context
    guidelines_ctx = ""
    if lender_id:
        engine = get_engine()
        doc = engine.get_guideline_doc(lender_id)
        if doc:
            guidelines_ctx = f"\n\nLENDER GUIDELINES ({lender_id}):\n{doc[:4000]}"
        else:
            # Full index as fallback
            idx = engine.get_index()
            guidelines_ctx = f"\n\nGUIDELINES INDEX:\n{json.dumps(idx, indent=2)[:3000]}"
    else:
        # Provide the full index for general questions
        engine = get_engine()
        idx = engine.get_index()
        guidelines_ctx = f"\n\nGUIDELINES INDEX:\n{json.dumps(idx, indent=2)[:3000]}"

    ctx_str = json.dumps(context) if context else ""
    user_content = message
    if ctx_str:
        user_content = f"Context:\n{ctx_str}\n\nQuestion: {message}"

    system = _SYSTEM_PROMPT + guidelines_ctx

    reply = chat(
        messages=[{"role": "user", "content": user_content}],
        system=system,
        model_tier="standard",
        max_tokens=600,
    )

    return jsonify({
        "user_id": user_id,
        "reply": reply,
        "agent": "Tranchi - Loan Processor",
    })


# ── POST /api/processor/fire-zap/ready-for-underwriting ──────────────────────

@processor_bp.route("/fire-zap/ready-for-underwriting", methods=["POST"])
@require_tranchi_auth
def fire_ready_zap():
    """
    Manually fire the ready_for_underwriting Zapier event for an application.

    Body: {"application_id": "app_abc123"}

    Useful when pre-UW was run earlier and the file is now ready to push to Arive.
    """
    body = request.get_json(force=True) or {}
    application_id = body.get("application_id", "")
    if not application_id:
        return jsonify({"error": "application_id required"}), 400

    with get_conn() as conn:
        row = fetchone(
            conn,
            "SELECT * FROM pre_underwriting_reports WHERE application_id = ? ORDER BY generated_at DESC LIMIT 1",
            (application_id,)
        )
    if not row:
        return jsonify({"error": "No pre-underwriting report found. Run POST /pre-underwrite first."}), 404

    from loan_officer.arive_zapier import fire_zap
    result = fire_zap(
        "ready_for_underwriting",
        {
            "application_id": application_id,
            "suggested_lender": row.get("suggested_lender", ""),
            "overall_status": row.get("overall_status", ""),
            "credit_memo": (row.get("credit_memo") or "")[:2000],
        },
        correlation_id=application_id,
    )

    return jsonify({
        "application_id": application_id,
        "zap_result": result,
    })
