"""
loan_officer/routes.py — Flask blueprint: /api/loan/*

All endpoints require Bearer auth matching TRANCHI_API_SECRET.
"""

from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from flask import Blueprint, request, jsonify, Response

from shared.auth import require_tranchi_auth
from shared.db import get_conn, insert, update, fetchone, fetchall
from shared.llm import chat
from shared.schemas import PrequalRequest, LoanApplicationRequest, ChatMessage
from shared.webhooks import verify_webhook

from loan_officer.prequal import (
    compute_dscr, compute_ltv, score_prequal,
    next_steps_for_product, _monthly_payment,
)
from loan_officer.lender_router import suggest_product
from loan_officer.document_collector import get_required_docs, missing_docs, docs_complete
from loan_officer.workflows import (
    transition, add_audit_event, parse_audit_log, state_summary
)
from loan_officer.lender_partners import get_lender
from loan_officer.arive_zapier import (
    fire_zap, to_arive_format, ARIVE_STATUS_MAP,
)

loan_bp = Blueprint("loan", __name__, url_prefix="/api/loan")

# Load system prompt once
_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()

_now = lambda: datetime.now(timezone.utc).isoformat()


# ── POST /api/loan/prequal ────────────────────────────────────────────────────

@loan_bp.route("/prequal", methods=["POST"])
@require_tranchi_auth
def create_prequal():
    """
    Pre-qualify a borrower for the best available loan product.

    Body: PrequalRequest (borrower + property + optional desired_product)
    Returns: prequal_id, score, suggested_product, monthly_payment_estimate, next_steps
    """
    body = request.get_json(force=True) or {}
    try:
        req = PrequalRequest.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    borrower = req.borrower
    prop = req.property

    # Route to best product
    routing = suggest_product(borrower, prop)
    product = routing["product"]

    # Compute DSCR if applicable
    dscr = None
    monthly_payment_est = 0.0

    loan_amount = borrower.desired_loan_amount or (
        prop.purchase_price * (1 - borrower.down_payment_pct / 100)
        if borrower.down_payment_pct > 0
        else prop.purchase_price * 0.75
    )

    rate_map = {"dscr": 8.0, "fix_flip": 10.5, "brrrr": 8.5, "conventional": 7.5, "hard_money": 11.0, "private": 12.0}
    est_rate = rate_map.get(product, 8.5)

    if product in ("dscr", "brrrr") and prop.monthly_rent > 0:
        monthly_payment_est = _monthly_payment(loan_amount, est_rate, 360)
        dscr = compute_dscr(
            monthly_rent=prop.monthly_rent,
            monthly_payment=monthly_payment_est,
            annual_taxes=prop.annual_taxes,
            annual_insurance=prop.annual_insurance,
            hoa_monthly=prop.hoa_monthly,
        )
    elif product in ("fix_flip", "hard_money"):
        monthly_payment_est = _monthly_payment(loan_amount, est_rate, 12)
    else:
        monthly_payment_est = _monthly_payment(loan_amount, est_rate, 360)

    ltv = None
    prop_value = prop.estimated_value or prop.purchase_price
    if prop_value > 0 and loan_amount > 0:
        ltv = compute_ltv(loan_amount, prop_value)

    # Fit score
    fit_score, strengths, concerns = score_prequal(borrower, prop, product, dscr=dscr, ltv=ltv)
    next_steps = next_steps_for_product(product)

    # Persist
    prequal_id = f"pq_{uuid.uuid4().hex[:12]}"
    now = _now()
    row = {
        "id": prequal_id,
        "user_id": borrower.user_id,
        "borrower_data": json.dumps(borrower.model_dump()),
        "property_data": json.dumps(prop.model_dump()),
        "score": fit_score,
        "suggested_product": product,
        "dscr": dscr,
        "ltv": ltv,
        "monthly_payment_estimate": round(monthly_payment_est, 2),
        "strengths": json.dumps(strengths),
        "concerns": json.dumps(concerns),
        "next_steps": json.dumps(next_steps),
        "status": "scored",
        "notes": req.notes,
        "created_at": now,
        "updated_at": now,
    }
    with get_conn() as conn:
        insert(conn, "loan_prequals", row)

    # ── Arive / Zapier outbound ────────────────────────────────────────────────
    arive_payload = to_arive_format(row, correlation_id=prequal_id)
    fire_zap("prequal_created", arive_payload, correlation_id=prequal_id)

    return jsonify({
        "prequal_id": prequal_id,
        "status": "scored",
        "score": fit_score,
        "suggested_product": product,
        "suggested_product_display": routing["display_name"],
        "qualifies": routing["qualifies"],
        "monthly_payment_estimate": round(monthly_payment_est, 2),
        "dscr": dscr,
        "ltv": ltv,
        "strengths": strengths,
        "concerns": concerns,
        "next_steps": next_steps,
        "alternatives": routing["alternatives"],
        "created_at": now,
    }), 201


# ── GET /api/loan/prequal/<prequal_id> ────────────────────────────────────────

@loan_bp.route("/prequal/<prequal_id>", methods=["GET"])
@require_tranchi_auth
def get_prequal(prequal_id: str):
    with get_conn() as conn:
        row = fetchone(conn, "SELECT * FROM loan_prequals WHERE id = ?", (prequal_id,))
    if not row:
        return jsonify({"error": "Prequal not found"}), 404
    # Decode JSON fields
    for field in ("borrower_data", "property_data", "strengths", "concerns", "next_steps"):
        if row.get(field):
            try:
                row[field] = json.loads(row[field])
            except Exception:
                pass
    return jsonify(row)


# ── POST /api/loan/application ────────────────────────────────────────────────

@loan_bp.route("/application", methods=["POST"])
@require_tranchi_auth
def create_application():
    """
    Open a formal loan application (requires a prequal_id).

    Returns: application_id, docs_required, current_state
    """
    body = request.get_json(force=True) or {}
    try:
        req = LoanApplicationRequest.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    # Load prequal
    with get_conn() as conn:
        prequal = fetchone(conn, "SELECT * FROM loan_prequals WHERE id = ?", (req.prequal_id,))
    if not prequal:
        return jsonify({"error": f"Prequal {req.prequal_id} not found"}), 404

    product = prequal["suggested_product"]
    docs_required = [d["doc_type"] for d in get_required_docs(product)]

    app_id = f"app_{uuid.uuid4().hex[:12]}"
    now = _now()
    initial_log = [
        {
            "event_type": "application_created",
            "actor": req.borrower.user_id,
            "payload": {"prequal_id": req.prequal_id, "product": product},
            "ts": now,
        }
    ]

    row = {
        "id": app_id,
        "prequal_id": req.prequal_id,
        "user_id": req.borrower.user_id,
        "status": "APP_STARTED",
        "current_state": "APP_STARTED",
        "lender_partner": "",
        "lender_ref_id": "",
        "docs_required": json.dumps(docs_required),
        "docs_received": "[]",
        "underwriter_notes": "",
        "approved_amount": None,
        "approved_rate": None,
        "approved_term": None,
        "conditions": "[]",
        "audit_log": json.dumps(initial_log),
        "created_at": now,
        "updated_at": now,
    }

    with get_conn() as conn:
        insert(conn, "loan_applications", row)

    # ── Arive / Zapier outbound ────────────────────────────────────────────────
    # Merge prequal data so to_arive_format has borrower+property context
    app_arive_row = {
        **row,
        "borrower_data": prequal["borrower_data"],
        "property_data": prequal["property_data"],
        "suggested_product": product,
        "dscr": prequal.get("dscr"),
        "monthly_payment_estimate": prequal.get("monthly_payment_estimate", 0),
    }
    fire_zap("application_submitted", to_arive_format(app_arive_row, correlation_id=app_id), correlation_id=app_id)

    return jsonify({
        "application_id": app_id,
        "prequal_id": req.prequal_id,
        "status": "APP_STARTED",
        "product": product,
        "docs_required": [
            {"doc_type": d["doc_type"], "label": d["label"]}
            for d in get_required_docs(product)
        ],
        "docs_missing": len(docs_required),
        "created_at": now,
    }), 201


# ── POST /api/loan/application/<id>/documents ─────────────────────────────────

@loan_bp.route("/application/<app_id>/documents", methods=["POST"])
@require_tranchi_auth
def upload_documents(app_id: str):
    """
    Record document references (S3 URLs) for an application.

    Body: {"documents": [{"doc_type": "...", "s3_url": "..."}]}
    """
    body = request.get_json(force=True) or {}
    docs = body.get("documents", [])
    if not docs:
        return jsonify({"error": "No documents provided"}), 400

    now = _now()
    with get_conn() as conn:
        app = fetchone(conn, "SELECT * FROM loan_applications WHERE id = ?", (app_id,))
        if not app:
            return jsonify({"error": "Application not found"}), 404

        docs_received = json.loads(app["docs_received"] or "[]")

        for doc in docs:
            doc_type = doc.get("doc_type", "")
            s3_url = doc.get("s3_url", "")
            if not doc_type or not s3_url:
                continue
            conn.execute(
                "INSERT INTO loan_documents (application_id, doc_type, s3_url, uploaded_at) VALUES (?, ?, ?, ?)",
                (app_id, doc_type, s3_url, now)
            )
            if doc_type not in docs_received:
                docs_received.append(doc_type)
            conn.commit()

        prequal = fetchone(conn, "SELECT suggested_product FROM loan_prequals WHERE id = ?",
                           (app["prequal_id"],))
        product = prequal["suggested_product"] if prequal else "dscr"

        still_missing = missing_docs(product, docs_received)
        new_state = app["current_state"]

        # Transition to APP_DOCS_PENDING if docs are still needed, or APP_SUBMITTED if complete
        if docs_complete(product, docs_received):
            new_state = "APP_SUBMITTED"
        elif app["current_state"] == "APP_STARTED":
            new_state = "APP_DOCS_PENDING"

        audit_log = parse_audit_log(app["audit_log"])
        audit_log = add_audit_event(audit_log, "docs_uploaded", {"count": len(docs)})
        if new_state != app["current_state"]:
            _, audit_log = transition(app["current_state"], new_state, audit_log)

        update(conn, "loan_applications",
               {
                   "docs_received": json.dumps(docs_received),
                   "current_state": new_state,
                   "status": new_state,
                   "audit_log": json.dumps(audit_log),
                   "updated_at": now,
               },
               "id = ?", (app_id,))

    # ── Arive / Zapier outbound ────────────────────────────────────────────────
    if docs_complete(product, docs_received):
        with get_conn() as conn:
            _prequal_for_docs = fetchone(conn, "SELECT * FROM loan_prequals WHERE id = ?",
                                         (app["prequal_id"],))
        if _prequal_for_docs:
            docs_arive_row = {
                **app,
                "id": app_id,
                "borrower_data": _prequal_for_docs["borrower_data"],
                "property_data": _prequal_for_docs["property_data"],
                "suggested_product": product,
                "dscr": _prequal_for_docs.get("dscr"),
                "monthly_payment_estimate": _prequal_for_docs.get("monthly_payment_estimate", 0),
            }
            fire_zap("documents_uploaded",
                     {**to_arive_format(docs_arive_row, correlation_id=app_id),
                      "docs_received": docs_received},
                     correlation_id=app_id)

    return jsonify({
        "application_id": app_id,
        "docs_received": docs_received,
        "missing": still_missing,
        "all_docs_received": docs_complete(product, docs_received),
        "current_state": new_state,
    })


# ── GET /api/loan/application/<id> ────────────────────────────────────────────

@loan_bp.route("/application/<app_id>", methods=["GET"])
@require_tranchi_auth
def get_application(app_id: str):
    with get_conn() as conn:
        app = fetchone(conn, "SELECT * FROM loan_applications WHERE id = ?", (app_id,))
        if not app:
            return jsonify({"error": "Application not found"}), 404
        docs = fetchall(conn, "SELECT * FROM loan_documents WHERE application_id = ?", (app_id,))

    prequal_id = app["prequal_id"]
    with get_conn() as conn:
        prequal = fetchone(conn, "SELECT * FROM loan_prequals WHERE id = ?", (prequal_id,))

    product = prequal["suggested_product"] if prequal else "dscr"
    docs_received = json.loads(app["docs_received"] or "[]")
    still_missing = missing_docs(product, docs_received)

    return jsonify({
        "application_id": app_id,
        "prequal_id": prequal_id,
        "user_id": app["user_id"],
        "status": app["status"],
        "current_state": app["current_state"],
        "state_info": state_summary(app["current_state"]),
        "lender_partner": app["lender_partner"],
        "lender_ref_id": app["lender_ref_id"],
        "product": product,
        "docs_required": get_required_docs(product),
        "docs_received": docs_received,
        "docs_missing": still_missing,
        "underwriter_notes": app["underwriter_notes"],
        "approved_amount": app["approved_amount"],
        "approved_rate": app["approved_rate"],
        "approved_term": app["approved_term"],
        "conditions": json.loads(app["conditions"] or "[]"),
        "documents": docs,
        "audit_log": parse_audit_log(app["audit_log"]),
        "created_at": app["created_at"],
        "updated_at": app["updated_at"],
    })


# ── POST /api/loan/application/<id>/route ─────────────────────────────────────

@loan_bp.route("/application/<app_id>/route", methods=["POST"])
@require_tranchi_auth
def route_to_lender(app_id: str):
    """
    Submit the application to a lender partner.

    Body: {"lender_slug": "kiavi"} (optional — uses best match if omitted)
    """
    body = request.get_json(force=True) or {}
    lender_slug = body.get("lender_slug", "")
    now = _now()

    with get_conn() as conn:
        app = fetchone(conn, "SELECT * FROM loan_applications WHERE id = ?", (app_id,))
        if not app:
            return jsonify({"error": "Application not found"}), 404

        if app["current_state"] not in ("APP_SUBMITTED", "APP_DOCS_PENDING", "APP_STARTED"):
            return jsonify({
                "error": f"Cannot route application in state {app['current_state']}",
                "allowed_states": ["APP_SUBMITTED", "APP_DOCS_PENDING"],
            }), 400

        prequal = fetchone(conn, "SELECT * FROM loan_prequals WHERE id = ?", (app["prequal_id"],))

    product = prequal["suggested_product"] if prequal else "dscr"

    # Auto-select lender if not specified
    if not lender_slug:
        from loan_officer.lender_partners import lenders_for_product
        candidates = lenders_for_product(product)
        lender_slug = candidates[0]["slug"] if candidates else "kiavi"

    lender = get_lender(lender_slug)
    if not lender:
        return jsonify({"error": f"Unknown lender: {lender_slug}"}), 400

    # TODO: integrate with lender X API
    # Real integration would POST to lender["api_endpoint"] with the application data,
    # receive a lender_ref_id, and store it for status tracking.
    print(f"[loan_router] TODO: POST application {app_id} to {lender['name']} at {lender['api_endpoint']}")
    lender_ref_id = f"STUB-{lender_slug.upper()}-{uuid.uuid4().hex[:6].upper()}"

    with get_conn() as conn:
        audit_log = parse_audit_log(app["audit_log"])
        _, audit_log = transition(app["current_state"], "UNDERWRITING", audit_log,
                                  payload={"lender": lender_slug, "lender_ref_id": lender_ref_id})
        update(conn, "loan_applications",
               {
                   "lender_partner": lender_slug,
                   "lender_ref_id": lender_ref_id,
                   "current_state": "UNDERWRITING",
                   "status": "UNDERWRITING",
                   "audit_log": json.dumps(audit_log),
                   "updated_at": now,
               },
               "id = ?", (app_id,))

    # ── Arive / Zapier outbound ────────────────────────────────────────────────
    if prequal:
        route_arive_row = {
            **app,
            "id": app_id,
            "borrower_data": prequal["borrower_data"],
            "property_data": prequal["property_data"],
            "suggested_product": product,
            "dscr": prequal.get("dscr"),
            "monthly_payment_estimate": prequal.get("monthly_payment_estimate", 0),
        }
        fire_zap("lender_routed",
                 {**to_arive_format(route_arive_row, correlation_id=app_id),
                  "lender": lender["name"],
                  "lender_slug": lender_slug,
                  "lender_ref_id": lender_ref_id},
                 correlation_id=app_id)

    return jsonify({
        "application_id": app_id,
        "lender": lender["name"],
        "lender_slug": lender_slug,
        "lender_ref_id": lender_ref_id,
        "status": "UNDERWRITING",
        "message": f"Application submitted to {lender['name']}. They typically respond within {lender['typical_close_days'].get(product, 21)} days.",
        "lender_contact": lender["contact"],
    })


# ── POST /api/loan/webhook/lender-update ──────────────────────────────────────

@loan_bp.route("/webhook/lender-update", methods=["POST"])
@verify_webhook(secret_env="LENDER_WEBHOOK_SECRET", header_name="X-Webhook-Signature")
def lender_webhook():
    """
    Lender partner pushes status updates here.

    Expected body:
    {
        "lender_ref_id": "STUB-KIAVI-ABC123",
        "status": "APPROVED" | "DECLINED" | "CONDITIONS",
        "approved_amount": 180000,
        "approved_rate": 7.75,
        "approved_term": 360,
        "conditions": ["Provide executed lease", "..."],
        "notes": "Strong deal — approved at requested amount."
    }
    """
    body = request.get_json(force=True) or {}
    lender_ref_id = body.get("lender_ref_id", "")
    new_status = body.get("status", "").upper()

    if not lender_ref_id:
        return jsonify({"error": "lender_ref_id required"}), 400
    if new_status not in ("APPROVED", "DECLINED", "CONDITIONS"):
        return jsonify({"error": f"Unknown status: {new_status}"}), 400

    now = _now()
    with get_conn() as conn:
        app = fetchone(conn, "SELECT * FROM loan_applications WHERE lender_ref_id = ?",
                       (lender_ref_id,))
        if not app:
            return jsonify({"error": f"No application found for lender_ref_id {lender_ref_id}"}), 404

        audit_log = parse_audit_log(app["audit_log"])
        _, audit_log = transition(app["current_state"], new_status, audit_log,
                                  actor="lender_webhook",
                                  payload={"lender_ref_id": lender_ref_id, **body})

        updates = {
            "status": new_status,
            "current_state": new_status,
            "audit_log": json.dumps(audit_log),
            "updated_at": now,
        }
        if new_status == "APPROVED":
            updates["approved_amount"] = body.get("approved_amount")
            updates["approved_rate"] = body.get("approved_rate")
            updates["approved_term"] = body.get("approved_term")
        if body.get("conditions"):
            updates["conditions"] = json.dumps(body["conditions"])
        if body.get("notes"):
            updates["underwriter_notes"] = body["notes"]

        update(conn, "loan_applications", updates, "id = ?", (app["id"],))

        # Log in audit table
        conn.execute(
            "INSERT INTO loan_audit_log (application_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            (app["id"], f"lender_status_{new_status.lower()}", json.dumps(body), now)
        )
        conn.commit()

    print(f"[lender_webhook] Application {app['id']} → {new_status} from lender ref {lender_ref_id}")

    return jsonify({"received": True, "application_id": app["id"], "new_status": new_status})


# ── POST /api/loan/webhook/arive-update ──────────────────────────────────────

@loan_bp.route("/webhook/arive-update", methods=["POST"])
@verify_webhook(secret_env="ARIVE_WEBHOOK_SECRET", header_name="X-Arive-Signature")
def arive_webhook():
    """
    Arive LOS pushes status updates via Zapier to this endpoint.

    Expected body:
    {
        "correlation_id": "app_abc123...",   # our application ID or hash
        "event_type": "status_change",       # Arive event type
        "status": "Cleared to Close",        # Arive status vocabulary
        "conditions": ["appraisal required", "..."],
        "notes": "UW approved with conditions."
    }

    We map Arive's status vocabulary to our internal state machine.
    When Arive is the source of truth (approved/declined/funded), we do NOT
    fire an outbound Zapier event back — that would create a loop.
    """
    body = request.get_json(force=True) or {}
    correlation_id = body.get("correlation_id", "")
    arive_status = body.get("status", "")
    conditions = body.get("conditions", [])
    notes = body.get("notes", "")

    if not correlation_id:
        return jsonify({"error": "correlation_id required"}), 400

    # Map Arive status to our state
    new_state = ARIVE_STATUS_MAP.get(arive_status, "")
    if not new_state:
        print(f"[arive_webhook] Unmapped Arive status '{arive_status}' — storing as note only")

    now = _now()

    # Look up application by correlation_id (we use app_id as correlation_id)
    with get_conn() as conn:
        app = fetchone(conn, "SELECT * FROM loan_applications WHERE id = ?", (correlation_id,))
        if not app:
            # Try lender_ref_id fallback (in case Arive passes their internal ID)
            app = fetchone(conn, "SELECT * FROM loan_applications WHERE lender_ref_id = ?",
                           (correlation_id,))
        if not app:
            return jsonify({
                "error": f"No application found for correlation_id {correlation_id}"
            }), 404

        app_id = app["id"]
        audit_log = parse_audit_log(app["audit_log"])

        updates: dict = {"updated_at": now}

        if new_state and new_state != app["current_state"]:
            try:
                _, audit_log = transition(
                    app["current_state"], new_state, audit_log,
                    actor="arive_webhook",
                    payload={"arive_status": arive_status, "correlation_id": correlation_id},
                )
                updates["current_state"] = new_state
                updates["status"] = new_state
            except ValueError as exc:
                # Log but don't fail — Arive may send statuses out of our expected order
                print(f"[arive_webhook] State transition error (continuing): {exc}")
                audit_log = add_audit_event(audit_log, "arive_status_note",
                                            {"arive_status": arive_status, "error": str(exc)})
        else:
            audit_log = add_audit_event(audit_log, "arive_status_received",
                                        {"arive_status": arive_status,
                                         "correlation_id": correlation_id})

        if conditions:
            updates["conditions"] = json.dumps(conditions)
        if notes:
            updates["underwriter_notes"] = notes
        updates["audit_log"] = json.dumps(audit_log)

        update(conn, "loan_applications", updates, "id = ?", (app_id,))

        conn.execute(
            "INSERT INTO loan_audit_log (application_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
            (app_id, "arive_status_update", json.dumps(body), now)
        )
        conn.commit()

    print(f"[arive_webhook] Application {app_id} Arive status='{arive_status}' → internal state='{new_state or 'unchanged'}'")

    return jsonify({
        "received": True,
        "application_id": app_id,
        "arive_status": arive_status,
        "internal_state": new_state or app["current_state"],
    })


# ── POST /api/loan/chat ───────────────────────────────────────────────────────

@loan_bp.route("/chat", methods=["POST"])
@require_tranchi_auth
def loan_chat():
    """
    Conversational interface for borrowers. Uses LLM with the loan officer system prompt.

    Body: {"user_id": "...", "message": "What is a DSCR loan?", "context": {...}}
    """
    body = request.get_json(force=True) or {}
    try:
        req = ChatMessage.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    ctx_str = json.dumps(req.context) if req.context else ""
    user_content = req.message
    if ctx_str:
        user_content = f"Context: {ctx_str}\n\nQuestion: {req.message}"

    reply = chat(
        messages=[{"role": "user", "content": user_content}],
        system=_SYSTEM_PROMPT,
        model_tier="standard",
        max_tokens=512,
    )

    return jsonify({
        "user_id": req.user_id,
        "reply": reply,
        "agent": "Tranchi - Loan Officer",
    })


# ── POST /api/loan/prequal-letter/<prequal_id> ────────────────────────────────

@loan_bp.route("/prequal-letter/<prequal_id>", methods=["POST"])
@require_tranchi_auth
def generate_prequal_letter(prequal_id: str):
    """
    Auto-generate the pre-qualification letter for a prequal.

    Liquidity is summed from classified bank_stmt rows in intake_documents
    (falls back to borrower self-reported liquidity, or to an `liquid_assets`
    override in the request body for LO manual control).

    Side effects:
      • Writes a `prequal_letters` audit row
      • Fires `prequal_letter_sent` Zapier hook (Gmail send + attachment)
        if borrower email is present and `skip_send` is not set

    Returns the inline base64 PDF + the computed range + breakdown.
    """
    from loan_officer.prequal_letter import generate_and_send

    body = request.get_json(silent=True) or {}
    try:
        result = generate_and_send(
            prequal_id,
            liquid_assets_override=(
                float(body["liquid_assets"]) if "liquid_assets" in body else None
            ),
            monthly_rent_override=(
                float(body["monthly_rent"]) if "monthly_rent" in body else None
            ),
            skip_send=bool(body.get("skip_send", False)),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"letter generation failed: {e}"}), 500

    return jsonify({
        "letter_id":          result.letter_id,
        "prequal_id":         result.prequal_id,
        "application_id":     result.application_id,
        "borrower_name":      result.borrower_name,
        "borrower_email":     result.borrower_email,
        "max_pp_low":         result.max_pp_low,
        "max_pp_high":        result.max_pp_high,
        "rate_low_pct":       result.rate_low_pct,
        "rate_high_pct":      result.rate_high_pct,
        "down_pct_low":       result.down_pct_low,
        "liquid_assets":      result.liquid_assets,
        "monthly_rent_used":  result.monthly_rent_used,
        "issued_at":          result.issued_at,
        "expires_at":         result.expires_at,
        "zap_fired":          result.zap_fired,
        "mcp_send_status":    result.mcp_send_status,
        "sent_to":            result.sent_to,
        "pdf_base64":         result.pdf_base64,
        "pdf_url":            result.pdf_url,
        "pdf_url_expires_at": result.pdf_url_expires_at,
        "breakdown":          result.breakdown,
    })


# ── GET /api/loan/prequal-letter/<letter_id>/pdf ──────────────────────────────

@loan_bp.route("/prequal-letter/<letter_id>/pdf", methods=["GET"])
def get_prequal_letter_pdf(letter_id: str):
    """
    Public, HMAC-tokenized endpoint that re-renders the letter PDF from its
    audit row. Used by Zapier (and the borrower's email client) as the
    attachment source — no Bearer auth, but the URL must carry a valid
    `token` + `exp` pair signed with TRANCHI_API_SECRET.

    Returns application/pdf bytes on success; 403 on bad/expired token; 404
    if the letter_id doesn't exist.
    """
    from loan_officer.prequal_letter import (
        verify_letter_pdf_token,
        regenerate_pdf_from_audit_row,
    )

    token = request.args.get("token", "")
    try:
        exp = int(request.args.get("exp", "0"))
    except ValueError:
        return jsonify({"error": "exp must be an integer unix timestamp"}), 400

    if not verify_letter_pdf_token(letter_id, exp, token):
        return jsonify({"error": "invalid or expired token"}), 403

    pdf = regenerate_pdf_from_audit_row(letter_id)
    if pdf is None:
        return jsonify({"error": "letter not found"}), 404

    return Response(
        pdf,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="prequal_letter_{letter_id}.pdf"',
            "Cache-Control": "private, max-age=600",
        },
    )


# ── GET /api/loan/prequal-letter/<letter_id> ──────────────────────────────────

@loan_bp.route("/prequal-letter/<letter_id>", methods=["GET"])
@require_tranchi_auth
def get_prequal_letter(letter_id: str):
    """
    Audit-row read. PDF bytes are not stored; if you need the file again,
    regenerate via POST (deterministic from the same intake state).
    """
    from loan_officer.prequal_letter import get_letter

    if not letter_id.startswith("pql_"):
        return jsonify({"error": "letter_id must start with 'pql_'"}), 400
    row = get_letter(letter_id)
    if row is None:
        return jsonify({"error": "letter not found"}), 404
    return jsonify(row)
