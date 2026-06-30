"""
tx_coordinator/parties.py — Party management for a transaction.

Tracks buyer, seller, both agents, title, escrow, inspector, lender, insurance.
Provides helpers for fetching parties by type and formatting contact info.
"""

from __future__ import annotations
from typing import Any, Optional

from shared.db import get_conn, fetchall, fetchone


PARTY_TYPES = [
    "buyer",
    "seller",
    "buyer_agent",
    "listing_agent",
    "title",
    "escrow",
    "inspector",
    "lender",
    "insurance",
    "other",
]


def get_parties(tx_id: str) -> list[dict]:
    with get_conn() as conn:
        return fetchall(conn, "SELECT * FROM tx_parties WHERE transaction_id = ?", (tx_id,))


def get_party_by_type(tx_id: str, party_type: str) -> Optional[dict]:
    with get_conn() as conn:
        rows = fetchall(
            conn,
            "SELECT * FROM tx_parties WHERE transaction_id = ? AND party_type = ? LIMIT 1",
            (tx_id, party_type)
        )
    return rows[0] if rows else None


def parties_summary(tx_id: str) -> dict[str, Any]:
    """Return a dict keyed by party_type with contact info."""
    parties = get_parties(tx_id)
    summary: dict[str, Any] = {}
    for p in parties:
        summary[p["party_type"]] = {
            "name": p["name"],
            "email": p["email"],
            "phone": p["phone"],
            "company": p["company"],
        }
    return summary


def missing_parties(tx_id: str) -> list[str]:
    """Return party types not yet added. Useful for on-boarding checklist."""
    existing = {p["party_type"] for p in get_parties(tx_id)}
    core_required = {"buyer", "seller", "title", "lender", "inspector"}
    return sorted(core_required - existing)
