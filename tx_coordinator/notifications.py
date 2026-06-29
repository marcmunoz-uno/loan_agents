"""
tx_coordinator/notifications.py — Deal-wide updates.

A transaction coordinator's actual day-job is keeping everyone on the deal
in the loop. We split the channel deliberately:

    Formal trail:   email posted inside the Arive loan file
                    → every tagged contact gets it
                    → archived inside the LOS (audit, compliance)

    Investor heads-up: short iMessage from tranchi-outbound-agent
                       → just the buyer/investor
                       → "Hey — just updated everyone on the file that X happened"

Both paths are audited separately into tx_outbound_messages so the operator
can replay either side without re-sending the other.

`notify_deal` is the single entry point. The auto-fire path (milestone
completion in NOTIFY_ON_COMPLETE_ALLOWLIST) and the manual path (chat tool,
POST /api/tx/<id>/notify) both call into it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db import get_conn, fetchone, insert
from shared.tranchi_client import OutboundClient
from shared.zapier_mcp import ZapierMCPClient

from tx_coordinator.arive_actions import post_loan_update
from tx_coordinator.parties import get_party_by_type

# Milestones that warrant a fan-out to everyone on the deal when completed.
# Everything else stays internal — bookkeeping completions (earnest money,
# inspection scheduled, etc.) don't need to spam the file.
NOTIFY_ON_COMPLETE_ALLOWLIST: set[str] = {
    "title_commitment_received",
    "clear_to_close",
    "closing_disclosure_received",
    "final_walkthrough",
    "closing_day",
}


# ── Public entry point ────────────────────────────────────────────────────────


def notify_deal(
    tx_id: str,
    *,
    event_summary: str,
    formal_subject: str,
    formal_body: str,
    investor_text: Optional[str] = None,
    zapier_client: Optional[ZapierMCPClient] = None,
    outbound_client: Optional[OutboundClient] = None,
) -> dict[str, Any]:
    """
    Fan an update out to everyone on the deal + ping the investor.

    Args:
        event_summary:   one-liner used for logs and as the default investor
                         text if `investor_text` isn't provided.
        formal_subject:  Arive email subject (goes to all loan contacts).
        formal_body:     Arive email body (plain text or HTML — whatever the
                         Arive Zap accepts; today it's passed through).
        investor_text:   the casual iMessage to the investor. If omitted, we
                         build "Hey — just updated everyone on the file that
                         {event_summary}".

    Returns:
        {
          "ok":            True iff at least the formal email was sent live OR
                           the dev-mode "skipped:..." path was hit cleanly,
          "tx_id":         str,
          "arive":         dict from post_loan_update,
          "investor":      dict { "ok", "status", "outbound_ref", "phone", "error" },
          "event_summary": echoed back so callers can build their own logs,
        }
    """
    arive_result = post_loan_update(
        tx_id,
        subject=formal_subject,
        body=formal_body,
        client=zapier_client,
    )

    investor_result = _notify_investor(
        tx_id,
        text=investor_text or f"Hey — just updated everyone on the file that {event_summary}.",
        event_summary=event_summary,
        client=outbound_client,
    )

    overall_ok = bool(arive_result.get("ok")) or arive_result.get("status", "").startswith("skipped:")
    return {
        "ok": overall_ok,
        "tx_id": tx_id,
        "event_summary": event_summary,
        "arive": arive_result,
        "investor": investor_result,
    }


def maybe_notify_on_completion(
    tx_id: str,
    milestone_name: str,
    *,
    zapier_client: Optional[ZapierMCPClient] = None,
    outbound_client: Optional[OutboundClient] = None,
) -> Optional[dict[str, Any]]:
    """
    If `milestone_name` is in NOTIFY_ON_COMPLETE_ALLOWLIST, fire `notify_deal`
    with a templated message. Otherwise return None (no-op).

    Called from both /api/tx/<id>/milestone/<name>/complete and the agent's
    complete_milestone tool so manual API ops and chat ops produce the same
    fan-out.
    """
    if milestone_name not in NOTIFY_ON_COMPLETE_ALLOWLIST:
        return None

    with get_conn() as conn:
        tx = fetchone(conn, "SELECT * FROM transactions WHERE id = ?", (tx_id,))
    if not tx:
        return None

    template = _TEMPLATES[milestone_name]
    subject = template["subject"].format(prop=tx["property_address"] or "the property")
    body = template["body"].format(
        prop=tx["property_address"] or "the property",
        closing_date=tx["closing_date"] or "",
        purchase_price=f"${tx['purchase_price']:,.0f}" if tx.get("purchase_price") else "",
    )
    return notify_deal(
        tx_id,
        event_summary=template["event_summary"],
        formal_subject=subject,
        formal_body=body,
        zapier_client=zapier_client,
        outbound_client=outbound_client,
    )


# ── Investor side ─────────────────────────────────────────────────────────────


def _notify_investor(
    tx_id: str,
    *,
    text: str,
    event_summary: str,
    client: Optional[OutboundClient],
) -> dict[str, Any]:
    """Send the casual heads-up text and audit it. Phone-less buyers get logged but not sent."""
    buyer = get_party_by_type(tx_id, "buyer")
    phone = (buyer or {}).get("phone") or ""

    if not phone:
        audit_id = _audit_investor(tx_id, body=text, mode="shadow",
                                   outbound_ref="", error="no_buyer_phone")
        return {"ok": False, "status": "skipped:no_buyer_phone",
                "outbound_ref": "", "phone": "", "error": "no_buyer_phone",
                "audit_row_id": audit_id}

    cli = client or OutboundClient()
    try:
        resp = cli.trigger_nurture(user_id=tx_id, phone=phone, context=text)
    except Exception as e:  # noqa: BLE001
        audit_id = _audit_investor(tx_id, body=text, mode="live",
                                   outbound_ref="", error=f"{type(e).__name__}: {e}")
        return {"ok": False, "status": f"failed:{type(e).__name__}",
                "outbound_ref": "", "phone": phone, "error": str(e)[:200],
                "audit_row_id": audit_id}

    err = resp.get("error") if isinstance(resp, dict) else ""
    ref = (resp.get("id") or resp.get("ref") or "") if isinstance(resp, dict) else ""
    mode = "live"
    if not getattr(cli, "configured", True):  # OutboundClient has no `configured`; this stays defensive
        mode = "shadow"
    audit_id = _audit_investor(tx_id, body=text, mode=mode,
                               outbound_ref=str(ref), error=str(err or ""))
    return {"ok": not err, "status": "sent" if not err else "failed:outbound_error",
            "outbound_ref": str(ref), "phone": phone, "error": str(err or ""),
            "audit_row_id": audit_id}


def _audit_investor(
    tx_id: str,
    *,
    body: str,
    mode: str,
    outbound_ref: str,
    error: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    buyer = get_party_by_type(tx_id, "buyer")
    with get_conn() as conn:
        return insert(conn, "tx_outbound_messages", {
            "transaction_id": tx_id,
            "party_id":       (buyer or {}).get("id"),
            "target_role":    "investor",
            "channel":        "imessage",
            "reason":         "deal_update_investor_heads_up",
            "body":           body[:4000],
            "mode":           mode,
            "outbound_ref":   outbound_ref,
            "sent_at":        now,
            "error":          error,
        })


# ── Templates per allowlisted milestone ───────────────────────────────────────

_TEMPLATES: dict[str, dict[str, str]] = {
    "title_commitment_received": {
        "subject": "Title commitment received — {prop}",
        "body": (
            "Team — the title commitment came in for {prop}. We're reviewing "
            "now and will flag anything that needs to be cured before "
            "{closing_date}. Reply here if you've already spotted a concern."
        ),
        "event_summary": "the title commitment came in",
    },
    "clear_to_close": {
        "subject": "Clear to Close — {prop}",
        "body": (
            "Team — lender just issued Clear to Close on {prop}. We're on "
            "track for closing on {closing_date}. Final walk-through and CD "
            "review are next."
        ),
        "event_summary": "the lender issued Clear to Close",
    },
    "closing_disclosure_received": {
        "subject": "Closing Disclosure received — {prop}",
        "body": (
            "Team — Closing Disclosure for {prop} is in. The 3-business-day "
            "RESPA review window starts now. Closing remains on {closing_date}."
        ),
        "event_summary": "the Closing Disclosure landed and the RESPA clock started",
    },
    "final_walkthrough": {
        "subject": "Final walk-through complete — {prop}",
        "body": (
            "Team — final walk-through for {prop} is complete. Reply here if "
            "anything surfaced that needs to be addressed before closing on "
            "{closing_date}."
        ),
        "event_summary": "the final walk-through wrapped up",
    },
    "closing_day": {
        "subject": "Closed — {prop}",
        "body": (
            "Team — {prop} closed today. Funds are wired, deed is signed, "
            "keys transferred. Thanks for the work on this one."
        ),
        "event_summary": "the deal closed",
    },
}
