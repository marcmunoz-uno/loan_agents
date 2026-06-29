"""
tx_coordinator/sweeper.py — Proactive deadline sweep.

The transaction coordinator is only valuable if it pushes. Endpoints answer
when asked; the sweeper does the opposite — it walks every open transaction
on a schedule (Render Cron or in-process scheduler), figures out which
deadlines warrant action, and either logs the action (shadow mode) or fires
it through tranchi-outbound-agent (live mode).

Modes
-----
The `TX_AGENT_MODE` env var picks the default behavior:

    shadow  — record the message we *would* send into tx_outbound_messages
              and stop. Nothing leaves the box. Use this until you trust
              the rules.
    live    — also POST to tranchi-outbound-agent so the investor / counter-
              party actually gets the iMessage / call.

A transaction can override the global setting via `transactions.agent_mode`.

Cooldown
--------
We never send the same (tx_id, reason) twice inside the cooldown window
(default 24h). Without this the sweep would re-fire every tick — the
sweep itself doesn't change the world that fast, so the underlying
deadline is still "urgent" the next time we look.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from shared.db import get_conn, fetchall, fetchone
from shared.tranchi_client import OutboundClient
from tx_coordinator.arive_actions import order_title_through_arive, sync_parties_from_arive
from tx_coordinator.deadline_engine import deadline_health_check
from tx_coordinator.parties import get_parties, get_party_by_type
from tx_coordinator.timeline import days_to_close

CONTACT_REFRESH_HOURS = int(os.environ.get("TX_SWEEPER_CONTACT_REFRESH_HOURS", "6"))

AgentMode = Literal["shadow", "live"]
DEFAULT_MODE: AgentMode = os.environ.get("TX_AGENT_MODE", "shadow")  # type: ignore[assignment]
COOLDOWN_HOURS = int(os.environ.get("TX_SWEEPER_COOLDOWN_HOURS", "24"))


@dataclass
class Escalation:
    """One thing the agent wants to do for one transaction on one tick."""
    transaction_id: str
    reason: str                   # short slug used as the dedupe key
    target_role: str              # investor | listing_agent | lender | title | inspector
    party_id: Optional[int]
    channel: str                  # imessage | sms | email | voice
    body: str

    def as_audit_row(self, mode: AgentMode, outbound_ref: str, error: str) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "party_id": self.party_id,
            "target_role": self.target_role,
            "channel": self.channel,
            "reason": self.reason,
            "body": self.body,
            "mode": mode,
            "outbound_ref": outbound_ref,
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "error": error,
        }


# ── Rule layer ────────────────────────────────────────────────────────────────


def build_escalations(tx_id: str) -> list[Escalation]:
    """
    Inspect a single transaction and return the list of escalations the
    rules want to fire right now. Caller is responsible for cooldown filtering.
    """
    with get_conn() as conn:
        tx = fetchone(conn, "SELECT * FROM transactions WHERE id = ?", (tx_id,))
    if not tx or tx["status"] != "open":
        return []

    out: list[Escalation] = []
    health = deadline_health_check(tx_id)
    dtc = days_to_close(tx["closing_date"])

    investor = _investor_party(tx_id)
    investor_phone = investor.get("phone") if investor else ""
    investor_id = investor.get("id") if investor else None

    for d in health["overdue"]:
        out.append(Escalation(
            transaction_id=tx_id,
            reason=f"{d['contingency_type']}_contingency_overdue",
            target_role="investor",
            party_id=investor_id,
            channel="imessage" if investor_phone else "email",
            body=(
                f"⚠️ {d['contingency_type'].title()} contingency was due "
                f"{abs(d['days_remaining'])} day(s) ago on {d['deadline_date']}. "
                f"You may be exposed under the PSA — confirm whether you've "
                f"already waived, objected, or need to request an extension."
            ),
        ))

    for d in health["urgent"]:
        out.append(Escalation(
            transaction_id=tx_id,
            reason=f"{d['contingency_type']}_contingency_urgent",
            target_role="investor",
            party_id=investor_id,
            channel="imessage" if investor_phone else "email",
            body=(
                f"Heads up — your {d['contingency_type']} contingency expires in "
                f"{d['days_remaining']} day(s) on {d['deadline_date']}. "
                f"Decide whether to waive, object, or extend before the deadline."
            ),
        ))

    # RESPA: closing disclosure must be in hand 3 business days before closing.
    # Closing in <= 3 days with no CD on file is escalation-worthy.
    if 0 <= dtc <= 3:
        cd_doc = _has_doc(tx_id, "closing_disclosure")
        if not cd_doc:
            out.append(Escalation(
                transaction_id=tx_id,
                reason="closing_disclosure_missing",
                target_role="investor",
                party_id=investor_id,
                channel="imessage" if investor_phone else "email",
                body=(
                    f"Closing is in {dtc} day(s) and we haven't logged a Closing "
                    f"Disclosure yet. RESPA requires the CD in your hands 3 "
                    f"business days before closing — chase the lender today."
                ),
            ))

    silent_lender = _silent_party(tx_id, "lender", days=7)
    if silent_lender:
        out.append(Escalation(
            transaction_id=tx_id,
            reason="lender_silent_7d",
            target_role="lender",
            party_id=silent_lender["id"],
            channel="voice",
            body=(
                f"No communication from {silent_lender['name']} for 7+ days. "
                f"Place a follow-up call confirming application status, "
                f"appraisal, and conditions outstanding."
            ),
        ))

    silent_listing = _silent_party(tx_id, "listing_agent", days=5)
    if silent_listing:
        out.append(Escalation(
            transaction_id=tx_id,
            reason="listing_agent_silent_5d",
            target_role="listing_agent",
            party_id=silent_listing["id"],
            channel="imessage" if silent_listing.get("phone") else "email",
            body=(
                f"Hi {silent_listing['name']} — checking in on outstanding items "
                f"for {tx['property_address']}. We're {dtc} day(s) from closing "
                f"and haven't connected in 5+ days."
            ),
        ))

    # Title-ordering through Arive. Standard timeline says title is ordered
    # by day 2 from PSA; if the milestone is still pending after that, the
    # agent fires arive.order_title (live) or logs the intent (shadow).
    psa_age_days = _days_since(tx.get("psa_execution_date") or tx.get("created_at"))
    if psa_age_days >= 2 and not _milestone_completed(tx_id, "title_ordered"):
        out.append(Escalation(
            transaction_id=tx_id,
            reason="title_ordered_via_arive",
            target_role="arive",
            party_id=None,
            channel="arive",
            body=(
                f"Trigger arive.order_title for {tx['property_address']} — "
                f"PSA executed {psa_age_days} day(s) ago, title milestone "
                f"still pending. Arive will forward to its configured "
                f"title provider."
            ),
        ))

    return out


# ── Dispatch ──────────────────────────────────────────────────────────────────


def run_sweep(
    *,
    mode: Optional[AgentMode] = None,
    cooldown_hours: int = COOLDOWN_HOURS,
    client: Optional[OutboundClient] = None,
) -> dict[str, Any]:
    """
    Walk every open transaction and act on whatever the rules flag.

    Returns a summary dict suitable for cron logs / a `/api/tx/sweep` response.
    """
    global_mode: AgentMode = mode or DEFAULT_MODE
    client = client or OutboundClient()

    with get_conn() as conn:
        open_txs = fetchall(
            conn,
            "SELECT id, agent_mode, arive_loan_id FROM transactions WHERE status = 'open'",
        )

    sent: list[dict] = []
    logged: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    contact_syncs: list[dict] = []

    for tx_row in open_txs:
        tx_id = tx_row["id"]
        tx_mode: AgentMode = tx_row.get("agent_mode") or global_mode  # type: ignore[assignment]

        # Refresh the party roster from Arive if it's been a while and we
        # have a loan id to look up. No-op when Zapier isn't configured.
        if tx_row.get("arive_loan_id") and _contacts_stale(tx_id, CONTACT_REFRESH_HOURS):
            sync_result = sync_parties_from_arive(tx_id)
            contact_syncs.append({"tx_id": tx_id, **sync_result})

        for esc in build_escalations(tx_id):
            if _on_cooldown(tx_id, esc.reason, cooldown_hours):
                skipped.append({"tx_id": tx_id, "reason": esc.reason, "cause": "cooldown"})
                continue

            outbound_ref = ""
            err = ""
            if tx_mode == "live":
                outbound_ref, err = _dispatch(esc, client)
                if err:
                    errors.append({"tx_id": tx_id, "reason": esc.reason, "error": err})

            _audit(esc, mode=tx_mode, outbound_ref=outbound_ref, error=err)

            (sent if tx_mode == "live" and not err else logged).append({
                "tx_id": tx_id,
                "reason": esc.reason,
                "target_role": esc.target_role,
                "channel": esc.channel,
            })

    return {
        "ok": True,
        "mode": global_mode,
        "transactions_scanned": len(open_txs),
        "actions_sent_live": len(sent),
        "actions_logged_shadow": len(logged),
        "actions_skipped_cooldown": len(skipped),
        "errors": errors,
        "sent": sent,
        "logged": logged,
        "skipped": skipped,
        "contact_syncs": contact_syncs,
        "swept_at": datetime.now(timezone.utc).isoformat(),
    }


def _contacts_stale(tx_id: str, hours: int) -> bool:
    """
    True if any 'arive' party hasn't been resynced in the last `hours`, or if
    no 'arive' parties exist yet (first-time sync).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        row = fetchone(
            conn,
            """SELECT COUNT(*) AS n FROM tx_parties
               WHERE transaction_id = ? AND source = 'arive'""",
            (tx_id,),
        )
        if (row or {}).get("n", 0) == 0:
            return True
        stale = fetchone(
            conn,
            """SELECT 1 FROM tx_parties
               WHERE transaction_id = ? AND source = 'arive'
                 AND (synced_at IS NULL OR synced_at < ?)
               LIMIT 1""",
            (tx_id, cutoff),
        )
    return stale is not None


# ── Internals ─────────────────────────────────────────────────────────────────


def _investor_party(tx_id: str) -> Optional[dict]:
    # The investor on Tranchi is the buyer.
    return get_party_by_type(tx_id, "buyer")


def _has_doc(tx_id: str, doc_type: str) -> bool:
    with get_conn() as conn:
        row = fetchone(
            conn,
            """SELECT 1 FROM tx_documents
               WHERE transaction_id = ? AND doc_type = ?
                 AND status NOT IN ('rejected', 'superseded') LIMIT 1""",
            (tx_id, doc_type),
        )
    return row is not None


def _milestone_completed(tx_id: str, milestone_name: str) -> bool:
    with get_conn() as conn:
        row = fetchone(
            conn,
            """SELECT 1 FROM tx_milestones
               WHERE transaction_id = ? AND milestone_name = ? AND status = 'completed'
               LIMIT 1""",
            (tx_id, milestone_name),
        )
    return row is not None


def _days_since(iso_date_or_dt: Optional[str]) -> int:
    """Days between today and the given ISO date/datetime. 0 if unparseable."""
    if not iso_date_or_dt:
        return 0
    try:
        # Accept either "2026-05-14" or "2026-05-14T12:34:56+00:00"
        date_str = iso_date_or_dt[:10]
        from datetime import date
        target = date.fromisoformat(date_str)
        return (date.today() - target).days
    except ValueError:
        return 0


def _silent_party(tx_id: str, party_type: str, days: int) -> Optional[dict]:
    """Return the party if they haven't communicated in `days` days."""
    party = get_party_by_type(tx_id, party_type)
    if not party:
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        last = fetchone(
            conn,
            """SELECT MAX(occurred_at) AS last
               FROM tx_communications
               WHERE transaction_id = ? AND party_id = ?""",
            (tx_id, party["id"]),
        )
    last_contact = (last or {}).get("last")
    if last_contact is None or last_contact < cutoff:
        return party
    return None


def _on_cooldown(tx_id: str, reason: str, hours: int) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        row = fetchone(
            conn,
            """SELECT 1 FROM tx_outbound_messages
               WHERE transaction_id = ? AND reason = ? AND sent_at > ?
               LIMIT 1""",
            (tx_id, reason, cutoff),
        )
    return row is not None


def _dispatch(esc: Escalation, client: OutboundClient) -> tuple[str, str]:
    """
    Hand the escalation to whichever downstream owns its channel. Returns
    (outbound_ref, error) — both are stored on the audit row.

    Channels:
        voice / imessage / sms / email → tranchi-outbound-agent
        arive                          → ZapierMCPClient via arive_actions
    """
    try:
        if esc.channel == "arive":
            # arive_actions already writes its own audit row; we still need
            # to return a ref/error so the sweeper's summary stays accurate.
            result = order_title_through_arive(esc.transaction_id)
            if not result.get("ok"):
                return "", result.get("status", "arive_unknown_error")
            return result.get("audit_row_id") and f"audit:{result['audit_row_id']}" or "sent", ""

        if esc.channel == "voice":
            resp = client.trigger_voice_call(
                user_id=esc.transaction_id,
                phone=_resolve_phone(esc),
                owner_name=esc.target_role,
                property_address="",
                context=esc.body,
            )
        else:
            resp = client.trigger_nurture(
                user_id=esc.transaction_id,
                phone=_resolve_phone(esc),
                context=esc.body,
            )
        if resp.get("error"):
            return "", str(resp["error"])
        return str(resp.get("id") or resp.get("ref") or ""), ""
    except Exception as e:  # noqa: BLE001 — sweep keeps going on individual failures
        return "", repr(e)


def _resolve_phone(esc: Escalation) -> str:
    if esc.party_id is None:
        return ""
    with get_conn() as conn:
        row = fetchone(conn, "SELECT phone FROM tx_parties WHERE id = ?", (esc.party_id,))
    return (row or {}).get("phone") or ""


def _audit(esc: Escalation, *, mode: AgentMode, outbound_ref: str, error: str) -> None:
    row = esc.as_audit_row(mode=mode, outbound_ref=outbound_ref, error=error)
    cols = ", ".join(row.keys())
    placeholders = ", ".join("?" for _ in row)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO tx_outbound_messages ({cols}) VALUES ({placeholders})",
            tuple(row.values()),
        )
        conn.commit()
