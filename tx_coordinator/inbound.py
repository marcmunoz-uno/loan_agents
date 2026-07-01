"""
tx_coordinator/inbound.py — handle replies FROM the investor.

Until now Sam only pushed. This closes the loop: when a buyer texts back
("already waived it", "I ordered title yesterday"), Sam figures out what they
mean, records it, stops nagging about it, and can relay it to anyone else on
the deal who asks.

Flow
----
1. Match the sender's phone to an open deal + party. Ambiguity (a buyer with
   several open deals) is resolved by the *most recent nudge we sent that
   number* — the reply is almost always answering the last thing we said.
2. Interpret the free-text reply against the deal's live context (open
   contingencies, pending milestones, the last nudge) into a structured intent.
3. Apply — ALWAYS log the reply to the communication record (that's what lets
   Sam relay it later), then:
     - resolve_contingency → resolve the deadline (stops the overdue nudge)
     - confirm_milestone   → complete it IF it's buyer-actionable. Milestones
                             only a lender/title company can trigger
                             (clear-to-close, CD received, title commitment)
                             are never advanced by a buyer text — logged +
                             flagged instead.
     - unclear/low-confidence → ask the buyer to clarify; change nothing.
4. Reply — text a confirmation (or the clarifying question) back. Reactive
   replies bypass the proactive daily cap but respect the kill switch, and only
   actually send when the deal is live (shadow mode logs the intended reply).

Remembering (steps 2–3) happens in ANY mode — Sam should learn what the buyer
said even in shadow. Only the outbound reply in step 4 is gated on live mode.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from shared.db import get_conn, fetchone, fetchall
from shared.llm import chat
from shared.tranchi_client import OutboundClient

from tx_coordinator import guardrails
from tx_coordinator.communication_hub import log_communication
from tx_coordinator.deadline_engine import resolve_deadline

# Milestones a BUYER can legitimately confirm by text. Everything else
# (clear_to_close, closing_disclosure_received, title_commitment_received,
# closing_day, loan_application_submitted, appraisal_completed, psa_executed)
# is driven by a lender/title/us — a buyer text never auto-advances those.
BUYER_ACTIONABLE_MILESTONES: set[str] = {
    "earnest_money_deposited",
    "inspection_scheduled",
    "inspection_completed",
    "inspection_response_deadline",
    "title_ordered",
    "appraisal_ordered",
    "financing_contingency_deadline",
    "title_contingency_deadline",
    "final_walkthrough",
}

CONTINGENCY_TYPES = {"inspection", "financing", "title"}
CONFIDENCE_THRESHOLD = 0.6

# Contingency milestone slug → deadline contingency_type, so completing the
# milestone and resolving the deadline stay in sync.
_MILESTONE_TO_CONTINGENCY = {
    "inspection_response_deadline": "inspection",
    "financing_contingency_deadline": "financing",
    "title_contingency_deadline": "title",
}


# ── Sender matching ───────────────────────────────────────────────────────────


def normalize_phone(phone: str) -> str:
    """Reduce a phone to its comparable digits (last 10 to dodge +1 / country code)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _match_deal(from_phone: str, tx_id: Optional[str]) -> Optional[dict]:
    """
    Find the (open) transaction + party this reply belongs to. Returns a dict
    with tx + party + the last nudge we sent that number, or None if unmatched.
    """
    target = normalize_phone(from_phone)
    if not target:
        return None

    with get_conn() as conn:
        rows = fetchall(
            conn,
            """SELECT p.id AS party_id, p.party_type, p.name, p.phone,
                      t.id AS tx_id, t.agent_mode, t.property_address
               FROM tx_parties p
               JOIN transactions t ON t.id = p.transaction_id
               WHERE t.status = 'open'""",
        )
    candidates = [
        r for r in rows
        if normalize_phone(r["phone"]) == target and (tx_id is None or r["tx_id"] == tx_id)
    ]
    if not candidates:
        return None

    # Disambiguate by the most recent outbound to this number.
    def last_contact(tx: str, party_id: int) -> str:
        with get_conn() as conn:
            row = fetchone(
                conn,
                """SELECT MAX(sent_at) AS last FROM tx_outbound_messages
                   WHERE transaction_id = ? AND (party_id = ? OR target_role = 'investor')""",
                (tx, party_id),
            )
        return (row or {}).get("last") or ""

    best = max(candidates, key=lambda r: last_contact(r["tx_id"], r["party_id"]))
    best["last_nudge"] = _last_nudge(best["tx_id"], best["party_id"])
    return best


def _last_nudge(tx_id: str, party_id: int) -> Optional[dict]:
    with get_conn() as conn:
        return fetchone(
            conn,
            """SELECT reason, body, channel, sent_at FROM tx_outbound_messages
               WHERE transaction_id = ? AND (party_id = ? OR target_role = 'investor')
               ORDER BY sent_at DESC LIMIT 1""",
            (tx_id, party_id),
        )


# ── Deal context + interpretation ─────────────────────────────────────────────


def _deal_context(tx_id: str) -> dict:
    with get_conn() as conn:
        active = fetchall(
            conn,
            "SELECT contingency_type FROM tx_deadlines WHERE transaction_id = ? AND status = 'active'",
            (tx_id,),
        )
        pending = fetchall(
            conn,
            """SELECT milestone_name FROM tx_milestones
               WHERE transaction_id = ? AND status = 'pending' ORDER BY sequence_order""",
            (tx_id,),
        )
    return {
        "active_contingencies": [r["contingency_type"] for r in active],
        "pending_milestones": [r["milestone_name"] for r in pending],
    }


_SYSTEM = (
    "You are the reply-interpreter for a real-estate transaction coordinator. "
    "A buyer/investor just texted back. Decide what they mean and return STRICT "
    "JSON only (no prose), with keys: "
    "intent (one of 'resolve_contingency','confirm_milestone','provide_info','question','unclear'), "
    "contingency_type (one of 'inspection','financing','title' or null), "
    "milestone_name (a milestone slug from the provided pending list, or null), "
    "confidence (0..1), summary (one past-tense sentence for the file), "
    "reply (a short, friendly confirmation OR clarifying question to send back)."
)


def _llm_interpret(text: str, context: dict, last_nudge: Optional[dict]) -> dict:
    prompt = (
        f"Buyer text: {text!r}\n"
        f"Last nudge we sent them: "
        f"{(last_nudge or {}).get('reason', 'none')} — {(last_nudge or {}).get('body', '')!r}\n"
        f"Open contingencies: {context['active_contingencies']}\n"
        f"Pending milestones (valid slugs): {context['pending_milestones']}\n"
        "Return the JSON now."
    )
    try:
        raw = chat(messages=[{"role": "user", "content": prompt}], system=_SYSTEM,
                   max_tokens=400, temperature=0.0)
        data = json.loads(_extract_json(raw))
    except Exception:
        return {"intent": "unclear", "contingency_type": None, "milestone_name": None,
                "confidence": 0.0, "summary": f"Buyer replied: {text[:160]}",
                "reply": "Thanks — could you clarify what that's in reference to?"}
    # Normalize / harden the model output.
    data.setdefault("intent", "unclear")
    data.setdefault("contingency_type", None)
    data.setdefault("milestone_name", None)
    data.setdefault("confidence", 0.0)
    data.setdefault("summary", f"Buyer replied: {text[:160]}")
    data.setdefault("reply", "Thanks — noted.")
    return data


def _extract_json(raw: str) -> str:
    start, end = raw.find("{"), raw.rfind("}")
    return raw[start:end + 1] if start != -1 and end != -1 else raw


# ── State application ─────────────────────────────────────────────────────────


def _complete_milestone(tx_id: str, name: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE tx_milestones SET status = 'completed', completed_at = ?
               WHERE transaction_id = ? AND milestone_name = ? AND status != 'completed'""",
            (now, tx_id, name),
        )
        conn.commit()
        if cur.rowcount == 0:
            return False
        nxt = fetchone(
            conn,
            """SELECT milestone_name FROM tx_milestones
               WHERE transaction_id = ? AND status = 'pending'
               ORDER BY sequence_order LIMIT 1""",
            (tx_id,),
        )
        if nxt:
            conn.execute(
                "UPDATE transactions SET current_milestone = ?, updated_at = ? WHERE id = ?",
                (nxt["milestone_name"], now, tx_id),
            )
            conn.commit()
    return True


def _deal_is_live(agent_mode: Optional[str]) -> bool:
    return os.environ.get("TX_AGENT_MODE", "shadow") == "live" and agent_mode == "live"


def _send_reply(tx_id: str, party: dict, text: str, *, agent_mode: Optional[str],
                client: Optional[OutboundClient]) -> dict:
    """
    Send a reactive reply back to the buyer. Bypasses the proactive daily cap +
    quiet hours (it's an answer, not a nudge) but respects the kill switch and
    only sends when the deal is live. Always logged to the communication record.
    """
    log_communication(tx_id, summary=f"[sam-reply] {text[:120]}", direction="out",
                       channel="imessage", party_id=party.get("party_id"), full_text=text)

    if not _deal_is_live(agent_mode):
        return {"sent": False, "reason": "shadow"}
    if guardrails.kill_switch_active():
        return {"sent": False, "reason": "kill_switch"}
    phone = party.get("phone") or ""
    if not phone:
        return {"sent": False, "reason": "no_phone"}

    cli = client or OutboundClient()
    try:
        resp = cli.trigger_nurture(user_id=tx_id, phone=phone, context=text)
    except Exception as e:  # noqa: BLE001
        return {"sent": False, "reason": f"error:{type(e).__name__}"}
    err = resp.get("error") if isinstance(resp, dict) else ""
    return {"sent": not err, "reason": "sent" if not err else "outbound_error"}


# ── Public entry point ────────────────────────────────────────────────────────


def handle_inbound_reply(
    from_phone: str,
    text: str,
    *,
    tx_id: Optional[str] = None,
    interpret: Optional[Callable[[str, dict, Optional[dict]], dict]] = None,
    client: Optional[OutboundClient] = None,
) -> dict[str, Any]:
    """
    Process one inbound reply. Returns a structured result for the webhook.
    `interpret` is injectable so tests don't need the LLM.
    """
    match = _match_deal(from_phone, tx_id)
    if not match:
        return {"ok": False, "status": "unmatched_sender", "from": from_phone}

    tx = match["tx_id"]
    context = _deal_context(tx)
    interp = (interpret or _llm_interpret)(text, context, match.get("last_nudge"))

    # Always record the raw inbound first — this is what makes relay possible.
    log_communication(tx, summary=f"[investor] {interp.get('summary', text[:160])}",
                       direction="in", channel="imessage",
                       party_id=match["party_id"], full_text=text)

    intent = interp.get("intent", "unclear")
    confidence = float(interp.get("confidence", 0.0) or 0.0)
    applied: dict[str, Any] = {"action": "none"}
    reply_text = interp.get("reply") or "Thanks — noted."

    low_confidence = confidence < CONFIDENCE_THRESHOLD
    if low_confidence or intent in ("unclear", "question"):
        # Ask the buyer to clarify; change nothing.
        applied = {"action": "clarify"}
        reply_text = interp.get("reply") or (
            "Thanks for the reply — just to make sure I log this correctly, what "
            "is that in reference to?"
        )
    elif intent == "resolve_contingency" and interp.get("contingency_type") in CONTINGENCY_TYPES:
        ct = interp["contingency_type"]
        resolved = resolve_deadline(tx, ct, actor="investor_reply")
        # keep the matching milestone in sync if present
        for slug, c in _MILESTONE_TO_CONTINGENCY.items():
            if c == ct:
                _complete_milestone(tx, slug)
        applied = {"action": "resolved_contingency", "contingency_type": ct, "changed": resolved}
    elif intent == "confirm_milestone" and interp.get("milestone_name"):
        name = interp["milestone_name"]
        if name in BUYER_ACTIONABLE_MILESTONES:
            done = _complete_milestone(tx, name)
            if name in _MILESTONE_TO_CONTINGENCY:
                resolve_deadline(tx, _MILESTONE_TO_CONTINGENCY[name], actor="investor_reply")
            applied = {"action": "completed_milestone", "milestone": name, "changed": done}
        else:
            # High-stakes milestone — record + flag, never auto-advance from a buyer text.
            applied = {"action": "flagged_for_human", "milestone": name}
            reply_text = interp.get("reply") or (
                "Thanks — I've noted that and will confirm it with the right party on the file."
            )
    else:  # provide_info
        applied = {"action": "logged_info"}

    reply_result = _send_reply(tx, match, reply_text,
                               agent_mode=match.get("agent_mode"), client=client)

    return {
        "ok": True,
        "tx_id": tx,
        "matched_party": {"party_id": match["party_id"], "type": match["party_type"],
                          "name": match["name"]},
        "intent": intent,
        "confidence": confidence,
        "applied": applied,
        "reply": {"text": reply_text, **reply_result},
        "summary": interp.get("summary"),
    }
