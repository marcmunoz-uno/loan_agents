"""
tx_coordinator/communication_hub.py — All communications across parties.

Logs inbound and outbound communications (email, SMS, iMessage, call, in-person).
Synthesizes a status update across all parties when asked.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db import get_conn, fetchall, fetchone, insert


def log_communication(
    tx_id: str,
    summary: str,
    direction: str = "out",
    channel: str = "email",
    party_id: Optional[int] = None,
    full_text: str = "",
    occurred_at: Optional[str] = None,
) -> int:
    """
    Insert a communication record. Returns the new row id.
    """
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "transaction_id": tx_id,
        "party_id": party_id,
        "direction": direction,
        "channel": channel,
        "summary": summary,
        "full_text": full_text,
        "occurred_at": occurred_at or now,
        "logged_at": now,
    }
    with get_conn() as conn:
        return insert(conn, "tx_communications", row)


def get_communications(tx_id: str, limit: int = 50) -> list[dict]:
    """Return recent communications for a transaction, newest first."""
    with get_conn() as conn:
        return fetchall(
            conn,
            """
            SELECT c.*, p.name as party_name, p.party_type
            FROM tx_communications c
            LEFT JOIN tx_parties p ON c.party_id = p.id
            WHERE c.transaction_id = ?
            ORDER BY c.occurred_at DESC
            LIMIT ?
            """,
            (tx_id, limit)
        )


def communications_by_party(tx_id: str, party_id: int) -> list[dict]:
    """Return all communications with a specific party."""
    with get_conn() as conn:
        return fetchall(
            conn,
            "SELECT * FROM tx_communications WHERE transaction_id = ? AND party_id = ? ORDER BY occurred_at DESC",
            (tx_id, party_id)
        )


def last_contact_per_party(tx_id: str) -> dict[str, Any]:
    """
    Return the most recent communication date for each party.
    Useful for flagging parties that have gone silent.
    """
    with get_conn() as conn:
        rows = fetchall(
            conn,
            """
            SELECT p.party_type, p.name, MAX(c.occurred_at) as last_contact
            FROM tx_parties p
            LEFT JOIN tx_communications c ON c.party_id = p.id AND c.transaction_id = ?
            WHERE p.transaction_id = ?
            GROUP BY p.id
            """,
            (tx_id, tx_id)
        )
    return {r["party_type"]: {"name": r["name"], "last_contact": r["last_contact"]} for r in rows}


def communication_summary(tx_id: str) -> str:
    """
    Generate a plain-language summary of recent communications for the chat interface.
    """
    comms = get_communications(tx_id, limit=10)
    if not comms:
        return "No communications logged yet for this transaction."

    lines = [f"Last {len(comms)} communications:"]
    for c in comms:
        party = c.get("party_name") or "Unknown party"
        lines.append(
            f"- [{c['occurred_at'][:10]}] {c['direction'].upper()} via {c['channel']} "
            f"with {party}: {c['summary']}"
        )
    return "\n".join(lines)
