"""
tx_coordinator/routes.py — Flask blueprint: /api/tx/*
"""

from __future__ import annotations
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from flask import Blueprint, request, jsonify

from shared.auth import require_tranchi_auth
from shared.db import get_conn, insert, update, fetchone, fetchall, execute
from shared.schemas import PSATerms, PartyType, MilestoneUpdate, DocumentRef, CommunicationLog, ChatMessage

from tx_coordinator import guardrails
from tx_coordinator.agent import run_agent_turn
from tx_coordinator.inbound import handle_inbound_reply
from tx_coordinator.communication_hub import (
    log_communication, get_communications, communication_summary
)
from tx_coordinator.deadline_engine import deadline_health_check, all_deadlines, upcoming_deadlines
from tx_coordinator.document_vault import document_checklist
from tx_coordinator.parties import parties_summary, missing_parties
from tx_coordinator.arive_actions import order_title_through_arive, sync_parties_from_arive
from tx_coordinator.notifications import notify_deal
from tx_coordinator.pdf_intake import accept_intake, extract_psa_terms
from tx_coordinator.sweeper import run_sweep
from tx_coordinator.timeline import generate_timeline, days_to_close, milestone_status_summary

tx_bp = Blueprint("tx", __name__, url_prefix="/api/tx")

_now = lambda: datetime.now(timezone.utc).isoformat()


# ── POST /api/tx/open ─────────────────────────────────────────────────────────

@tx_bp.route("/open", methods=["POST"])
@require_tranchi_auth
def open_transaction():
    """
    Open a new transaction from PSA terms.

    Body: PSATerms + user_id
    Returns: tx_id, generated timeline
    """
    body = request.get_json(force=True) or {}
    user_id = body.pop("user_id", None) or body.get("buyer", {}).get("user_id", "unknown")
    intake_id = body.pop("intake_id", None)
    arive_loan_id = (body.pop("arive_loan_id", None) or "").strip()

    try:
        psa = PSATerms.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    tx_id = f"tx_{uuid.uuid4().hex[:12]}"
    now = _now()

    # Generate timeline
    milestones = generate_timeline(psa)

    row = {
        "id": tx_id,
        "user_id": user_id,
        "psa_terms": psa.model_dump_json(),
        "purchase_price": psa.purchase_price,
        "closing_date": psa.closing_date,
        "psa_execution_date": psa.psa_execution_date,  # may be NULL — defaults to created_at
        "arive_loan_id": arive_loan_id,  # may be empty — set later via /link-arive-loan
        "status": "open",
        "current_milestone": "psa_executed",
        "property_address": psa.property_address,
        "buyer_name": psa.buyer_name,
        "seller_name": psa.seller_name,
        "notes": psa.notes,
        "created_at": now,
        "updated_at": now,
    }

    with get_conn() as conn:
        insert(conn, "transactions", row)

        # Insert milestones
        for m in milestones:
            conn.execute(
                """INSERT INTO tx_milestones
                   (transaction_id, milestone_name, milestone_label, sequence_order,
                    target_date, status, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (tx_id, m["name"], m["label"], m["sequence"],
                 m["target_date"], "pending", "")
            )
        conn.commit()

        # Insert core contingency deadlines
        contingency_milestones = [m for m in milestones if m.get("is_contingency")]
        for m in contingency_milestones:
            conn.execute(
                """INSERT INTO tx_deadlines
                   (transaction_id, contingency_type, deadline_date, status)
                   VALUES (?, ?, ?, 'active')""",
                (tx_id, m["contingency_type"], m["target_date"])
            )
        conn.commit()

        # Seed buyer and seller as parties
        for party_type, name, email, phone in [
            ("buyer", psa.buyer_name, psa.buyer_email, psa.buyer_phone),
            ("seller", psa.seller_name, psa.seller_email, psa.seller_phone),
            ("buyer_agent", psa.buyer_agent_name, "", ""),
            ("listing_agent", psa.listing_agent_name, "", ""),
        ]:
            if name:
                conn.execute(
                    """INSERT INTO tx_parties
                       (transaction_id, party_type, name, email, phone, added_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (tx_id, party_type, name, email, phone, now)
                )
        conn.commit()

    if intake_id is not None:
        accept_intake(int(intake_id), tx_id)

    # If we know the Arive loan id, immediately mirror the party roster.
    # Returns silently if Zapier isn't configured — see arive_actions.
    sync_summary = sync_parties_from_arive(tx_id) if arive_loan_id else None

    return jsonify({
        "tx_id": tx_id,
        "status": "open",
        "arive_loan_id": arive_loan_id,
        "arive_sync": sync_summary,
        "property_address": psa.property_address,
        "purchase_price": psa.purchase_price,
        "closing_date": psa.closing_date,
        "days_to_close": days_to_close(psa.closing_date),
        "timeline": milestones,
        "contingency_deadlines": [
            {
                "type": m["contingency_type"],
                "deadline": m["target_date"],
            }
            for m in milestones if m.get("is_contingency")
        ],
        "created_at": now,
    }), 201


# ── GET /api/tx/<tx_id> ───────────────────────────────────────────────────────

@tx_bp.route("/<tx_id>", methods=["GET"])
@require_tranchi_auth
def get_transaction(tx_id: str):
    with get_conn() as conn:
        tx = fetchone(conn, "SELECT * FROM transactions WHERE id = ?", (tx_id,))
    if not tx:
        return jsonify({"error": "Transaction not found"}), 404

    with get_conn() as conn:
        milestones = fetchall(
            conn,
            "SELECT * FROM tx_milestones WHERE transaction_id = ? ORDER BY sequence_order",
            (tx_id,)
        )

    deadline_health = deadline_health_check(tx_id)
    milestone_summary = milestone_status_summary(milestones)
    party_summary = parties_summary(tx_id)
    missing = missing_parties(tx_id)

    try:
        psa_terms = json.loads(tx["psa_terms"])
    except Exception:
        psa_terms = {}

    return jsonify({
        "tx_id": tx_id,
        "status": tx["status"],
        "property_address": tx["property_address"],
        "purchase_price": tx["purchase_price"],
        "closing_date": tx["closing_date"],
        "days_to_close": days_to_close(tx["closing_date"]),
        "current_milestone": tx["current_milestone"],
        "milestone_summary": milestone_summary,
        "deadline_health": {
            "health": deadline_health["health"],
            "overdue_count": len(deadline_health["overdue"]),
            "urgent_count": len(deadline_health["urgent"]),
        },
        "parties": party_summary,
        "missing_parties": missing,
        "milestones": milestones,
        "psa_terms": psa_terms,
        "created_at": tx["created_at"],
        "updated_at": tx["updated_at"],
    })


# ── POST /api/tx/<tx_id>/milestone/<milestone_name>/complete ──────────────────

@tx_bp.route("/<tx_id>/milestone/<milestone_name>/complete", methods=["POST"])
@require_tranchi_auth
def complete_milestone(tx_id: str, milestone_name: str):
    body = request.get_json(force=True) or {}
    try:
        req = MilestoneUpdate.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    now = _now()
    completed_at = req.completed_at or now

    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE tx_milestones
               SET status = 'completed', completed_at = ?, notes = ?
               WHERE transaction_id = ? AND milestone_name = ?""",
            (completed_at, req.notes, tx_id, milestone_name)
        )
        conn.commit()

        if cur.rowcount == 0:
            return jsonify({"error": f"Milestone '{milestone_name}' not found for transaction {tx_id}"}), 404

        # Update transaction's current_milestone to the next pending one
        next_pending = fetchone(
            conn,
            """SELECT milestone_name FROM tx_milestones
               WHERE transaction_id = ? AND status = 'pending'
               ORDER BY sequence_order LIMIT 1""",
            (tx_id,)
        )
        if next_pending:
            conn.execute(
                "UPDATE transactions SET current_milestone = ?, updated_at = ? WHERE id = ?",
                (next_pending["milestone_name"], now, tx_id)
            )
            conn.commit()

    # Auto-resolve contingency deadline if this is a contingency milestone
    contingency_map = {
        "inspection_response_deadline": "inspection",
        "financing_contingency_deadline": "financing",
        "title_contingency_deadline": "title",
    }
    if milestone_name in contingency_map:
        from tx_coordinator.deadline_engine import resolve_deadline
        resolve_deadline(tx_id, contingency_map[milestone_name])

    # Fan an update out to everyone on the deal + investor heads-up if the
    # milestone is on the allowlist (title commitment, CTC, CD, walk-through, closing).
    from tx_coordinator.notifications import maybe_notify_on_completion
    notify_result = maybe_notify_on_completion(tx_id, milestone_name)

    payload = {
        "tx_id": tx_id,
        "milestone": milestone_name,
        "status": "completed",
        "completed_at": completed_at,
    }
    if notify_result is not None:
        payload["notified"] = {
            "event_summary": notify_result["event_summary"],
            "arive_status":  notify_result["arive"]["status"],
            "investor_status": notify_result["investor"]["status"],
        }
    return jsonify(payload)


# ── POST /api/tx/<tx_id>/party ────────────────────────────────────────────────

@tx_bp.route("/<tx_id>/party", methods=["POST"])
@require_tranchi_auth
def add_party(tx_id: str):
    body = request.get_json(force=True) or {}
    try:
        req = PartyType.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    now = _now()
    with get_conn() as conn:
        # Check transaction exists
        tx = fetchone(conn, "SELECT id FROM transactions WHERE id = ?", (tx_id,))
        if not tx:
            return jsonify({"error": "Transaction not found"}), 404

        cur = conn.execute(
            """INSERT INTO tx_parties
               (transaction_id, party_type, name, email, phone, company, contact_data, added_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tx_id, req.party_type, req.name, req.email, req.phone,
             req.company, json.dumps({"notes": req.notes}), now)
        )
        conn.commit()
        party_id = cur.lastrowid

    return jsonify({
        "tx_id": tx_id,
        "party_id": party_id,
        "party_type": req.party_type,
        "name": req.name,
    }), 201


# ── GET /api/tx/<tx_id>/deadlines ────────────────────────────────────────────

@tx_bp.route("/<tx_id>/deadlines", methods=["GET"])
@require_tranchi_auth
def get_deadlines(tx_id: str):
    health = deadline_health_check(tx_id)
    upcoming = upcoming_deadlines(tx_id, days_ahead=14)

    return jsonify({
        "tx_id": tx_id,
        "health": health["health"],
        "overdue": health["overdue"],
        "urgent": health["urgent"],
        "approaching": health["approaching"],
        "upcoming_14_days": upcoming,
        "all": health["all"],
    })


# ── POST /api/tx/<tx_id>/document ─────────────────────────────────────────────

@tx_bp.route("/<tx_id>/document", methods=["POST"])
@require_tranchi_auth
def add_document(tx_id: str):
    body = request.get_json(force=True) or {}
    try:
        req = DocumentRef.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    now = _now()
    with get_conn() as conn:
        tx = fetchone(conn, "SELECT id FROM transactions WHERE id = ?", (tx_id,))
        if not tx:
            return jsonify({"error": "Transaction not found"}), 404

        cur = conn.execute(
            """INSERT INTO tx_documents
               (transaction_id, doc_type, s3_url, party_uploaded, status, notes, uploaded_at)
               VALUES (?, ?, ?, ?, 'received', ?, ?)""",
            (tx_id, req.doc_type, req.s3_url, req.party_uploaded, req.notes, now)
        )
        conn.commit()
        doc_id = cur.lastrowid

    checklist = document_checklist(tx_id)

    return jsonify({
        "tx_id": tx_id,
        "doc_id": doc_id,
        "doc_type": req.doc_type,
        "status": "received",
        "document_completion_pct": checklist["completion_pct"],
    }), 201


# ── POST /api/tx/<tx_id>/communication ───────────────────────────────────────

@tx_bp.route("/<tx_id>/communication", methods=["POST"])
@require_tranchi_auth
def log_comm(tx_id: str):
    body = request.get_json(force=True) or {}
    try:
        req = CommunicationLog.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    comm_id = log_communication(
        tx_id=tx_id,
        summary=req.summary,
        direction=req.direction,
        channel=req.channel,
        party_id=req.party_id,
        full_text=req.full_text,
        occurred_at=req.occurred_at,
    )

    return jsonify({
        "tx_id": tx_id,
        "comm_id": comm_id,
        "summary": req.summary,
        "logged": True,
    }), 201


# ── POST /api/tx/<tx_id>/chat ─────────────────────────────────────────────────

@tx_bp.route("/<tx_id>/chat", methods=["POST"])
@require_tranchi_auth
def tx_chat(tx_id: str):
    """
    Conversational interface with tool use. Sam can fetch live state, mark
    milestones complete, log communications, and (with user approval) send
    iMessages or place voice calls via tranchi-outbound-agent.
    """
    body = request.get_json(force=True) or {}
    try:
        req = ChatMessage.model_validate(body)
    except Exception as e:
        return jsonify({"error": "Validation error", "detail": str(e)}), 400

    with get_conn() as conn:
        tx = fetchone(conn, "SELECT id FROM transactions WHERE id = ?", (tx_id,))
    if not tx:
        return jsonify({"error": "Transaction not found"}), 404

    try:
        result = run_agent_turn(tx_id=tx_id, user_message=req.message)
    except RuntimeError as e:
        return jsonify({"error": "agent_unavailable", "detail": str(e)}), 503

    return jsonify({
        "tx_id": tx_id,
        "user_id": req.user_id,
        "reply": result["reply"],
        "tool_calls": result["tool_calls"],
        "iterations": result["iterations"],
        "agent": "Tranchi - Transaction Coordinator",
    })


# ── POST /api/tx/sweep ────────────────────────────────────────────────────────

@tx_bp.route("/sweep", methods=["POST"])
@require_tranchi_auth
def tx_sweep():
    """
    Proactive deadline sweep. Hit by Render Cron every 15 min (or invoked
    manually for debugging). Respects TX_AGENT_MODE for shadow/live dispatch.
    """
    body = request.get_json(silent=True) or {}
    mode_override = body.get("mode")  # "shadow" | "live" | None
    summary = run_sweep(mode=mode_override)
    return jsonify(summary)


# ── POST /api/tx/open-from-pdf ────────────────────────────────────────────────

@tx_bp.route("/open-from-pdf", methods=["POST"])
@require_tranchi_auth
def tx_open_from_pdf():
    """
    Phase 1 of the two-phase PSA flow. Accepts a PDF (multipart or raw body),
    runs Claude vision extraction, persists the candidate terms, and returns
    them so the caller can review/correct and then call /api/tx/open with
    intake_id set.
    """
    pdf_bytes: bytes = b""
    source_url = ""

    if "file" in request.files:
        f = request.files["file"]
        pdf_bytes = f.read()
        source_url = f.filename or ""
    elif request.is_json:
        payload = request.get_json(force=True) or {}
        source_url = payload.get("source_url", "")
        # Not fetching remote URLs here — that belongs to a dedicated intake worker.
        return jsonify({"error": "remote_source_url_not_supported_yet",
                        "hint": "POST the PDF as multipart 'file' for now."}), 400
    else:
        pdf_bytes = request.get_data() or b""

    if not pdf_bytes:
        return jsonify({"error": "empty_pdf"}), 400

    result = extract_psa_terms(pdf_bytes, source_url=source_url)
    status = 200 if result["extraction_status"] == "extracted" else 422
    return jsonify(result), status


# ── POST /api/tx/<tx_id>/notify ───────────────────────────────────────────────

@tx_bp.route("/<tx_id>/notify", methods=["POST"])
@require_tranchi_auth
def tx_notify(tx_id: str):
    """
    Fan an update out to everyone on the deal + investor heads-up.

    Body:
        {
          "event_summary":  "the repair request was accepted",
          "formal_subject": "...",
          "formal_body":    "...",
          "investor_text":  "..."   (optional override)
        }
    """
    body = request.get_json(force=True) or {}
    required = ("event_summary", "formal_subject", "formal_body")
    missing = [k for k in required if not body.get(k)]
    if missing:
        return jsonify({"error": "missing_fields", "fields": missing}), 400

    result = notify_deal(
        tx_id,
        event_summary=body["event_summary"],
        formal_subject=body["formal_subject"],
        formal_body=body["formal_body"],
        investor_text=body.get("investor_text"),
    )
    return jsonify(result), 200 if result["ok"] else 502


# ── POST /api/tx/<tx_id>/sync-arive-contacts ──────────────────────────────────

@tx_bp.route("/<tx_id>/sync-arive-contacts", methods=["POST"])
@require_tranchi_auth
def tx_sync_arive_contacts(tx_id: str):
    """
    Pull the party roster from Arive's `list_loan_contacts` and upsert into
    tx_parties. Agent-added parties are preserved.
    """
    result = sync_parties_from_arive(tx_id)
    status = 200 if result["ok"] else (409 if result["status"].startswith("skipped:") else 502)
    return jsonify(result), status


# ── POST /api/tx/<tx_id>/link-arive-loan ──────────────────────────────────────

@tx_bp.route("/<tx_id>/link-arive-loan", methods=["POST"])
@require_tranchi_auth
def tx_link_arive_loan(tx_id: str):
    """
    Attach an Arive loan id to an existing transaction and immediately sync
    the party roster. Use this when the LO side (loan_agents) creates the
    Arive loan after this tx was already opened.
    """
    body = request.get_json(force=True) or {}
    loan_id = (body.get("arive_loan_id") or "").strip()
    if not loan_id:
        return jsonify({"error": "arive_loan_id required"}), 400

    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET arive_loan_id = ?, updated_at = ? WHERE id = ?",
            (loan_id, now, tx_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Transaction not found"}), 404

    sync = sync_parties_from_arive(tx_id)
    return jsonify({"tx_id": tx_id, "arive_loan_id": loan_id, "arive_sync": sync})


# ── POST /api/tx/<tx_id>/order-title ──────────────────────────────────────────

@tx_bp.route("/<tx_id>/order-title", methods=["POST"])
@require_tranchi_auth
def tx_order_title(tx_id: str):
    """
    Fire the Arive `order_title` Zapier action for this transaction.

    There is no direct title-company integration in this stack — title is
    ordered through Arive (the LO's LOS). Arive forwards the order to its
    configured title provider, then status updates flow back via the existing
    `/api/loan/webhook/arive-update` endpoint in loan_agents (not duplicated
    here).

    Inputs in the body are optional — everything Arive needs is derived from
    the transaction + PSA + parties. Pass `force=true` to bypass the
    "milestone already completed" guard.
    """
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force", False))
    result = order_title_through_arive(tx_id, force=force)
    status = 200 if result.get("ok") else (409 if result.get("status", "").startswith("skipped:already") else 422)
    return jsonify(result), status


# ── Live-mode guardrails (operator controls) ──────────────────────────────────

@tx_bp.route("/guardrails", methods=["GET"])
@require_tranchi_auth
def tx_guardrails():
    """Snapshot of the live-send guardrails: kill switch, cap usage, channels, quiet hours."""
    return jsonify({"ok": True, **guardrails.config_summary()})


@tx_bp.route("/pause", methods=["POST"])
@require_tranchi_auth
def tx_pause():
    """
    KILL SWITCH — instantly stop ALL live outbound across every deal. Takes
    effect on the next sweep tick (no redeploy). Shadow logging continues.
    """
    guardrails.set_kill_switch(True)
    return jsonify({"ok": True, "kill_switch_active": True})


@tx_bp.route("/resume", methods=["POST"])
@require_tranchi_auth
def tx_resume():
    """Clear the kill switch — live sending resumes (subject to the other guardrails)."""
    guardrails.set_kill_switch(False)
    return jsonify({"ok": True, "kill_switch_active": False})


@tx_bp.route("/<tx_id>/go-live", methods=["POST"])
@require_tranchi_auth
def tx_go_live(tx_id: str):
    """
    Opt a single transaction into live sending. Sam still only sends when the
    GLOBAL TX_AGENT_MODE is also 'live' and the per-send guardrails pass.
    """
    now = _now()
    with get_conn() as conn:
        # Only an OPEN deal can be opted live — flipping a closed/cancelled deal
        # live is meaningless (the sweep skips non-open deals) and leaves stale
        # state. Distinguish "not found" from "not open" for a clear error.
        tx = fetchone(conn, "SELECT status FROM transactions WHERE id = ?", (tx_id,))
        if not tx:
            return jsonify({"error": "Transaction not found"}), 404
        if tx["status"] != "open":
            return jsonify({"error": f"Transaction is {tx['status']}, not open", "status": tx["status"]}), 409
        conn.execute(
            "UPDATE transactions SET agent_mode = 'live', updated_at = ? WHERE id = ?",
            (now, tx_id),
        )
        conn.commit()
    return jsonify({"ok": True, "tx_id": tx_id, "agent_mode": "live"})


@tx_bp.route("/<tx_id>/go-shadow", methods=["POST"])
@require_tranchi_auth
def tx_go_shadow(tx_id: str):
    """Opt a single transaction back out of live sending (returns it to shadow)."""
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE transactions SET agent_mode = 'shadow', updated_at = ? WHERE id = ?",
            (now, tx_id),
        )
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Transaction not found"}), 404
    return jsonify({"ok": True, "tx_id": tx_id, "agent_mode": "shadow"})


# ── Inbound reply webhook (Blooio / Zapier-fired) ─────────────────────────────


def _inbound_auth_ok() -> bool:
    """
    Authenticate the inbound-reply webhook. Accepts, in order:
      1. Authorization: Bearer <secret>   (preferred — keeps the secret out of URLs)
      2. X-Webhook-Secret: <secret>       (preferred)
      3. ?secret=<…> query param          (webhook-URL friendly, but logged in access logs)
    Uses TX_INBOUND_SECRET, falling back to TRANCHI_API_SECRET so the endpoint
    is never accidentally wide open. Read at request time so the secret can be
    rotated without a restart.
    """
    expected = os.environ.get("TX_INBOUND_SECRET", "") or os.environ.get("TRANCHI_API_SECRET", "")
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth.split(" ", 1)[1], expected):
        return True
    if hmac.compare_digest(request.headers.get("X-Webhook-Secret", ""), expected):
        return True
    if hmac.compare_digest(request.args.get("secret", ""), expected):
        return True
    return False


@tx_bp.route("/webhook/inbound", methods=["POST"])
def tx_inbound_reply():
    """
    Receive a reply FROM a party (typically the investor) forwarded by the
    outbound channel (Blooio iMessage via tranchi-outbound-agent, or a Zapier
    hook). Sam matches it to the deal, interprets it, records it, applies any
    resulting state change, and replies.

    Body (flexible field names): from_phone|from|phone, text|body|message,
    optional tx_id. Auth via bearer / X-Webhook-Secret / ?secret= (see above)
    rather than the standard operator auth, since the sender is a webhook.
    """
    if not _inbound_auth_ok():
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    phone = body.get("from_phone") or body.get("from") or body.get("phone") or ""
    text = body.get("text") or body.get("body") or body.get("message") or ""
    tx_id = body.get("tx_id")
    if not phone or not text:
        return jsonify({"error": "missing_fields", "need": ["from_phone", "text"]}), 400

    result = handle_inbound_reply(phone, text, tx_id=tx_id)
    status = 200 if result.get("ok") else 202  # 202: accepted but unmatched (don't make the sender retry)
    return jsonify(result), status
