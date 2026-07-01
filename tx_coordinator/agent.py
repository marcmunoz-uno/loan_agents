"""
tx_coordinator/agent.py — Tool-calling conversational layer for "Sam".

The /chat endpoint used to stuff transaction context into a string and ask
Claude to describe it. That made Sam an explainer, not a coordinator. This
module gives Sam tools so a single message can do work: complete a milestone,
log a call, add a party, send an iMessage.

The loop is the standard Anthropic tool-use loop:

    user → assistant (text + tool_use blocks)
       → for each tool_use: dispatch + tool_result
       → assistant → ... → end_turn

We cap iterations (`MAX_TURNS`) so a hallucinating model can't spin forever.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from shared.db import get_conn, fetchone, fetchall
from shared.llm import chat_with_tools
from shared.tranchi_client import OutboundClient
from tx_coordinator.arive_actions import order_title_through_arive
from tx_coordinator.communication_hub import log_communication, communication_summary
from tx_coordinator.deadline_engine import deadline_health_check, all_deadlines
from tx_coordinator.notifications import maybe_notify_on_completion, notify_deal
from tx_coordinator.parties import parties_summary, get_party_by_type
from tx_coordinator.sweeper import build_escalations
from tx_coordinator.timeline import days_to_close, milestone_status_summary

SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"

MAX_TURNS = 6  # safety cap on tool-use iterations

# ── Tool schemas ──────────────────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_status",
        "description": (
            "Pull the live snapshot of the transaction: closing date, days to close, "
            "current milestone, milestone progress, deadline health, and missing parties. "
            "Always call this first if you don't already have the context you need."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_deadlines",
        "description": "Return all contingency deadlines with warning levels.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_parties",
        "description": "Return contact info for every party on the transaction.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_pending_escalations",
        "description": (
            "Return the escalations the proactive sweeper *would* fire right now "
            "if it ran. Useful for answering 'what do I need to do today?'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "complete_milestone",
        "description": (
            "Mark a milestone as completed. Use only when the user explicitly says "
            "they've done the underlying thing (e.g. 'I wired the earnest money')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "milestone_name": {
                    "type": "string",
                    "description": "Slug from the milestone list — e.g. earnest_money_deposited.",
                },
                "notes": {"type": "string"},
            },
            "required": ["milestone_name"],
        },
    },
    {
        "name": "log_communication",
        "description": (
            "Record a communication with a party. Use this when the user tells you "
            "they spoke / emailed / texted someone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "direction": {"type": "string", "enum": ["in", "out"]},
                "channel": {
                    "type": "string",
                    "enum": ["email", "sms", "imessage", "call", "in_person", "portal"],
                },
                "party_type": {
                    "type": "string",
                    "description": (
                        "Which party — buyer | seller | buyer_agent | listing_agent | "
                        "title | escrow | inspector | lender | insurance"
                    ),
                },
                "full_text": {"type": "string"},
            },
            "required": ["summary", "direction", "channel"],
        },
    },
    {
        "name": "send_outbound_message",
        "description": (
            "Send an iMessage or place a voice call to a party via tranchi-outbound-agent. "
            "Only call this when the user explicitly approves the message — never preemptively. "
            "Confirm the message body in your reply before invoking the tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "party_type": {
                    "type": "string",
                    "description": (
                        "Recipient role: buyer | seller | listing_agent | lender | title | "
                        "escrow | inspector | insurance"
                    ),
                },
                "channel": {"type": "string", "enum": ["imessage", "sms", "voice"]},
                "body": {"type": "string"},
            },
            "required": ["party_type", "channel", "body"],
        },
    },
    {
        "name": "order_title_via_arive",
        "description": (
            "Fire the Arive `order_title` Zapier action for this transaction. "
            "Use this when the user explicitly approves placing the title order, "
            "or when the standard timeline says title should already be ordered. "
            "Arive forwards the order to its configured title provider — there "
            "is no separate title-company API to hit. Idempotent: a second call "
            "without `force=true` returns skipped:already_ordered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Re-order title even if a successful order already exists. Default false.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "notify_deal",
        "description": (
            "Send an update to everyone on the deal. Posts a formal email "
            "inside the Arive loan file (every tagged contact receives it + "
            "it's archived in the LOS) AND sends a casual iMessage to the "
            "investor that says 'just updated everyone on the file that "
            "<event_summary>'. Use this for events the whole team needs to "
            "know about — title commitment landed, repair request answered, "
            "wire instructions confirmed, schedule change. Do NOT use for "
            "milestone completions on the auto-allowlist (title_commitment_"
            "received, clear_to_close, closing_disclosure_received, "
            "final_walkthrough, closing_day) — those fan out automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_summary": {
                    "type": "string",
                    "description": (
                        "Short past-tense phrase used in the investor's heads-up text. "
                        "Examples: 'the repair request was accepted', 'closing was "
                        "pushed to Friday', 'wire instructions arrived from First American'."
                    ),
                },
                "formal_subject": {
                    "type": "string",
                    "description": "Subject of the Arive loan-file email.",
                },
                "formal_body": {
                    "type": "string",
                    "description": (
                        "Body of the Arive loan-file email — plain text or simple HTML. "
                        "Address the team; everyone tagged on the loan will get it."
                    ),
                },
                "investor_text": {
                    "type": "string",
                    "description": (
                        "Optional override for the investor's iMessage. If omitted "
                        "we send 'Hey — just updated everyone on the file that "
                        "{event_summary}'."
                    ),
                },
            },
            "required": ["event_summary", "formal_subject", "formal_body"],
        },
    },
]


# ── Tool dispatch ─────────────────────────────────────────────────────────────


def _tool_get_status(tx_id: str) -> dict:
    with get_conn() as conn:
        tx = fetchone(conn, "SELECT * FROM transactions WHERE id = ?", (tx_id,))
        milestones = fetchall(
            conn,
            "SELECT * FROM tx_milestones WHERE transaction_id = ? ORDER BY sequence_order",
            (tx_id,),
        )
    if not tx:
        return {"error": f"Transaction {tx_id} not found"}

    health = deadline_health_check(tx_id)
    return {
        "tx_id": tx_id,
        "status": tx["status"],
        "property_address": tx["property_address"],
        "purchase_price": tx["purchase_price"],
        "closing_date": tx["closing_date"],
        "psa_execution_date": tx.get("psa_execution_date"),
        "days_to_close": days_to_close(tx["closing_date"]),
        "current_milestone": tx["current_milestone"],
        "milestone_summary": milestone_status_summary(milestones),
        "deadline_health": health["health"],
        "overdue_count": len(health["overdue"]),
        "urgent_count": len(health["urgent"]),
        "recent_communications": communication_summary(tx_id),
    }


def _tool_get_deadlines(tx_id: str) -> dict:
    return {"deadlines": all_deadlines(tx_id)}


def _tool_list_parties(tx_id: str) -> dict:
    return {"parties": parties_summary(tx_id)}


def _tool_list_pending_escalations(tx_id: str) -> dict:
    pending = build_escalations(tx_id)
    return {
        "count": len(pending),
        "escalations": [
            {
                "reason": e.reason,
                "target_role": e.target_role,
                "channel": e.channel,
                "body": e.body,
            }
            for e in pending
        ],
    }


def _tool_complete_milestone(tx_id: str, milestone_name: str, notes: str = "") -> dict:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE tx_milestones
               SET status = 'completed', completed_at = ?, notes = ?
               WHERE transaction_id = ? AND milestone_name = ?""",
            (now, notes, tx_id, milestone_name),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"error": f"Milestone {milestone_name!r} not found"}
        next_pending = fetchone(
            conn,
            """SELECT milestone_name FROM tx_milestones
               WHERE transaction_id = ? AND status = 'pending'
               ORDER BY sequence_order LIMIT 1""",
            (tx_id,),
        )
        if next_pending:
            conn.execute(
                "UPDATE transactions SET current_milestone = ?, updated_at = ? WHERE id = ?",
                (next_pending["milestone_name"], now, tx_id),
            )
            conn.commit()
    notify_result = maybe_notify_on_completion(tx_id, milestone_name)
    return {
        "ok": True,
        "completed": milestone_name,
        "next_pending": (next_pending or {}).get("milestone_name"),
        "fanned_out": notify_result is not None,
        "notify": (
            {
                "event_summary":  notify_result["event_summary"],
                "arive_status":   notify_result["arive"]["status"],
                "investor_status": notify_result["investor"]["status"],
            } if notify_result else None
        ),
    }


def _tool_log_communication(
    tx_id: str,
    summary: str,
    direction: str,
    channel: str,
    party_type: Optional[str] = None,
    full_text: str = "",
) -> dict:
    party_id = None
    if party_type:
        party = get_party_by_type(tx_id, party_type)
        if party:
            party_id = party["id"]
    comm_id = log_communication(
        tx_id=tx_id,
        summary=summary,
        direction=direction,
        channel=channel,
        party_id=party_id,
        full_text=full_text,
    )
    return {"ok": True, "comm_id": comm_id, "party_id": party_id}


def _tool_send_outbound_message(
    tx_id: str,
    party_type: str,
    channel: str,
    body: str,
    *,
    client: Optional[OutboundClient] = None,
) -> dict:
    party = get_party_by_type(tx_id, party_type)
    if not party:
        return {"error": f"No party of type {party_type!r} on this transaction"}
    phone = party.get("phone") or ""
    if not phone and channel in ("imessage", "sms", "voice"):
        return {"error": f"Party {party_type!r} has no phone on file"}

    client = client or OutboundClient()
    if channel == "voice":
        resp = client.trigger_voice_call(
            user_id=tx_id,
            phone=phone,
            owner_name=party.get("name", ""),
            property_address="",
            context=body,
        )
    else:
        resp = client.trigger_nurture(user_id=tx_id, phone=phone, context=body)

    # Always log the communication so the audit trail matches reality.
    log_communication(
        tx_id=tx_id,
        summary=f"[agent-sent] {body[:120]}",
        direction="out",
        channel=channel,
        party_id=party["id"],
        full_text=body,
    )
    return {"ok": not resp.get("error"), "outbound_response": resp}


def _tool_order_title_via_arive(tx_id: str, force: bool = False) -> dict:
    return order_title_through_arive(tx_id, force=force)


def _tool_notify_deal(
    tx_id: str,
    event_summary: str,
    formal_subject: str,
    formal_body: str,
    investor_text: Optional[str] = None,
) -> dict:
    return notify_deal(
        tx_id,
        event_summary=event_summary,
        formal_subject=formal_subject,
        formal_body=formal_body,
        investor_text=investor_text,
    )


def _dispatch(name: str, tx_id: str, args: dict) -> dict:
    if name == "get_status":
        return _tool_get_status(tx_id)
    if name == "get_deadlines":
        return _tool_get_deadlines(tx_id)
    if name == "list_parties":
        return _tool_list_parties(tx_id)
    if name == "list_pending_escalations":
        return _tool_list_pending_escalations(tx_id)
    if name == "complete_milestone":
        return _tool_complete_milestone(tx_id, **args)
    if name == "log_communication":
        return _tool_log_communication(tx_id, **args)
    if name == "send_outbound_message":
        return _tool_send_outbound_message(tx_id, **args)
    if name == "order_title_via_arive":
        return _tool_order_title_via_arive(tx_id, **args)
    if name == "notify_deal":
        return _tool_notify_deal(tx_id, **args)
    return {"error": f"Unknown tool: {name}"}


# ── Public entry point ────────────────────────────────────────────────────────


def run_agent_turn(tx_id: str, user_message: str) -> dict[str, Any]:
    """
    One conversational turn against Sam, with tool-use enabled.

    Returns:
        {
          "reply":          final assistant text (stripped),
          "tool_calls":     list of {name, input, output} for the UI to show,
          "iterations":     int,
        }
    """
    system_prompt = SYSTEM_PROMPT_PATH.read_text()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

    tool_trace: list[dict] = []

    for turn in range(MAX_TURNS):
        resp = chat_with_tools(
            messages=messages,
            system=system_prompt,
            tools=TOOLS,
            max_tokens=2048,
        )

        # Append the assistant turn back into the conversation verbatim.
        messages.append({"role": "assistant", "content": resp["content"]})

        if resp["stop_reason"] != "tool_use":
            text = _extract_text(resp["content"])
            return {
                "reply": text.strip(),
                "tool_calls": tool_trace,
                "iterations": turn + 1,
                "stop_reason": resp["stop_reason"],
            }

        tool_results = []
        for block in resp["content"]:
            if block.get("type") != "tool_use":
                continue
            name = block["name"]
            args = block.get("input", {}) or {}
            output = _dispatch(name, tx_id, args)
            tool_trace.append({"name": name, "input": args, "output": output})
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(output),
            })

        messages.append({"role": "user", "content": tool_results})

    return {
        "reply": "[Agent hit the tool-use turn cap. Restart the conversation.]",
        "tool_calls": tool_trace,
        "iterations": MAX_TURNS,
        "stop_reason": "max_turns",
    }


def _extract_text(content: list[dict]) -> str:
    return "".join(b.get("text", "") for b in content if b.get("type") == "text")
