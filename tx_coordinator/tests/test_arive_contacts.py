"""
Tests for the Arive contact-sync surface in tx_coordinator.arive_actions.

The role-map and upsert behavior are what the rest of the system depends on:
- silent-party detection (sweeper) reads tx_parties by party_type
- escalation message routing (sweeper._dispatch) reads phone/email from tx_parties
- Sam-added parties must survive every sync
"""

from __future__ import annotations

import json

import pytest

from shared.db import get_conn, fetchall, fetchone
from tx_coordinator.arive_actions import (
    ARIVE_ROLE_MAP,
    sync_parties_from_arive,
)


class StubZapier:
    def __init__(self, contacts=None, configured=True, raise_exc=None):
        self.configured = configured
        self.calls: list[dict] = []
        self._raise = raise_exc
        self._contacts = contacts or [
            {"arive_contact_id": "AR-001", "role": "Borrower",
             "name": "Marc Munoz", "email": "marc@munoz.ltd", "phone": "+13135550100",
             "company": ""},
            {"arive_contact_id": "AR-002", "role": "Listing Agent",
             "name": "Bob Williams", "email": "bob@kw.com", "phone": "+13135551111",
             "company": "Keller Williams"},
            {"arive_contact_id": "AR-003", "role": "Title Company",
             "name": "First American Title", "email": "ops@firstam.com",
             "phone": "+13135552222", "company": "First American"},
        ]

    def execute(self, *, app, action, mode, params, instructions, output):
        self.calls.append({"app": app, "action": action, "params": params})
        if self._raise:
            raise self._raise
        return {"content": [{"text": json.dumps({"contacts": self._contacts})}]}


def _set_arive_loan(tx_id: str, loan_id: str = "loan_AR_001") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET arive_loan_id = ? WHERE id = ?",
            (loan_id, tx_id),
        )
        conn.commit()


# ── Role map ──────────────────────────────────────────────────────────────────


def test_role_map_covers_critical_arive_labels():
    for label in ("Borrower", "Listing Agent", "Title Company", "Inspector", "Lender"):
        assert label in ARIVE_ROLE_MAP


# ── Sync behavior ─────────────────────────────────────────────────────────────


def test_sync_skips_when_no_arive_loan_id(insert_transaction):
    tx_id = insert_transaction
    result = sync_parties_from_arive(tx_id, client=StubZapier())
    assert result["status"] == "skipped:no_arive_loan_id"


def test_sync_skips_when_zapier_unconfigured(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    result = sync_parties_from_arive(tx_id, client=StubZapier(configured=False))
    assert result["status"] == "skipped:zapier_mcp_not_configured"


def test_sync_upserts_arive_contacts(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    result = sync_parties_from_arive(tx_id, client=StubZapier())
    assert result["ok"] is True
    assert result["synced_count"] == 3

    with get_conn() as conn:
        arive_parties = fetchall(
            conn,
            "SELECT * FROM tx_parties WHERE transaction_id = ? AND source = 'arive' ORDER BY arive_contact_id",
            (tx_id,),
        )
    assert {p["arive_contact_id"] for p in arive_parties} == {"AR-001", "AR-002", "AR-003"}
    by_id = {p["arive_contact_id"]: p for p in arive_parties}
    assert by_id["AR-001"]["party_type"] == "buyer"
    assert by_id["AR-002"]["party_type"] == "listing_agent"
    assert by_id["AR-003"]["party_type"] == "title"
    for p in arive_parties:
        assert p["synced_at"]  # timestamp set


def test_sync_does_not_clobber_agent_added_party(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)

    # Sam adds an inspector via chat — not in Arive yet.
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tx_parties
               (transaction_id, party_type, name, email, phone, source, added_at)
               VALUES (?, 'inspector', 'Sam-added Inspector', 'i@x.com', '+13135559999', 'agent', '2026-05-20T00:00:00Z')""",
            (tx_id,),
        )
        conn.commit()

    sync_parties_from_arive(tx_id, client=StubZapier())

    with get_conn() as conn:
        agent_parties = fetchall(
            conn,
            "SELECT * FROM tx_parties WHERE transaction_id = ? AND source = 'agent' AND party_type = 'inspector'",
            (tx_id,),
        )
    assert len(agent_parties) == 1
    assert agent_parties[0]["name"] == "Sam-added Inspector"


def test_sync_is_idempotent_updates_in_place(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    stub = StubZapier()

    first = sync_parties_from_arive(tx_id, client=stub)
    second = sync_parties_from_arive(tx_id, client=stub)

    assert first["synced_count"] == 3
    assert second["synced_count"] == 3
    # No duplicate rows.
    with get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM tx_parties WHERE transaction_id = ? AND source = 'arive'",
            (tx_id,),
        ).fetchone()["n"]
    assert n == 3


def test_sync_updates_changed_fields(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    sync_parties_from_arive(tx_id, client=StubZapier())

    # Second call with updated contact info — phone changed.
    new_contacts = [
        {"arive_contact_id": "AR-001", "role": "Borrower",
         "name": "Marc Munoz", "email": "marc@munoz.ltd", "phone": "+13135559876",
         "company": ""},
    ]
    sync_parties_from_arive(tx_id, client=StubZapier(contacts=new_contacts))

    with get_conn() as conn:
        row = fetchone(
            conn,
            "SELECT phone FROM tx_parties WHERE arive_contact_id = ? AND transaction_id = ?",
            ("AR-001", tx_id),
        )
    assert row["phone"] == "+13135559876"


def test_sync_handles_zapier_exception(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    result = sync_parties_from_arive(tx_id, client=StubZapier(raise_exc=RuntimeError("boom")))
    assert result["ok"] is False
    assert "failed:" in result["status"]


def test_unknown_role_falls_to_other(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    stub = StubZapier(contacts=[
        {"arive_contact_id": "AR-999", "role": "Photographer", "name": "Random Person",
         "email": "p@example.com", "phone": "", "company": ""},
    ])
    sync_parties_from_arive(tx_id, client=stub)
    with get_conn() as conn:
        row = fetchone(
            conn,
            "SELECT party_type FROM tx_parties WHERE arive_contact_id = ?",
            ("AR-999",),
        )
    assert row["party_type"] == "other"
