"""
Tests for tx_coordinator.arive_actions.

ZapierMCPClient is stubbed so the suite doesn't require a real Zapier
endpoint. The shape of the stub mirrors the actual client closely enough
that swapping in the real one is a single-line change.
"""

from __future__ import annotations

import json

import pytest

from shared.db import get_conn, fetchall, fetchone
from tx_coordinator import arive_actions
from tx_coordinator.arive_actions import build_title_order_params, order_title_through_arive


class StubZapier:
    """ZapierMCPClient replacement that records calls and returns canned responses."""

    def __init__(self, response=None, raise_exc=None, configured=True):
        self.calls: list[dict] = []
        self._response = response or {"content": [{"text": '{"title_order_id": "TO-12345"}'}]}
        self._raise = raise_exc
        self.configured = configured

    def execute(self, *, app, action, mode, params, instructions, output):
        self.calls.append({"app": app, "action": action, "mode": mode, "params": params})
        if self._raise is not None:
            raise self._raise
        return self._response


def test_params_includes_psa_anchored_dates(insert_transaction):
    tx_id = insert_transaction
    with get_conn() as conn:
        tx = fetchone(conn, "SELECT * FROM transactions WHERE id = ?", (tx_id,))
        parties_rows = fetchall(conn, "SELECT * FROM tx_parties WHERE transaction_id = ?", (tx_id,))
    parties = {p["party_type"]: p for p in parties_rows}
    psa = json.loads(tx["psa_terms"])

    params = build_title_order_params(tx=tx, psa=psa, parties=parties)

    assert params["crmReferenceId"] == tx_id
    assert params["loanPurpose"] == "Purchase"
    assert params["estimatedClosingDate"] == "2026-06-13"
    assert params["contractExecutionDate"] == "2026-05-14"
    assert params["purchasePriceOrEstimatedValue"] == 95000.0
    assert params["earnestMoneyDepositAmount"] == 2500.0
    assert params["borrower1_firstName"] == "Marc"
    assert params["borrower1_lastName"] == "Munoz"
    assert params["subjectProperty_state"] == "MI"
    assert params["subjectProperty_postalCode"] == "48224"


def test_order_title_sends_when_configured(insert_transaction):
    tx_id = insert_transaction
    stub = StubZapier()
    result = order_title_through_arive(tx_id, client=stub)

    assert result["ok"] is True
    assert result["status"] == "sent"
    assert len(stub.calls) == 1
    assert stub.calls[0]["app"] == "arive"
    assert stub.calls[0]["action"] == "order_title"
    assert stub.calls[0]["params"]["crmReferenceId"] == tx_id


def test_order_title_is_idempotent(insert_transaction):
    tx_id = insert_transaction
    stub = StubZapier()
    first = order_title_through_arive(tx_id, client=stub)
    second = order_title_through_arive(tx_id, client=stub)

    assert first["status"] == "sent"
    assert second["status"] == "skipped:already_ordered"
    assert len(stub.calls) == 1


def test_order_title_force_re_orders(insert_transaction):
    tx_id = insert_transaction
    stub = StubZapier()
    order_title_through_arive(tx_id, client=stub)
    forced = order_title_through_arive(tx_id, force=True, client=stub)

    assert forced["status"] == "sent"
    assert len(stub.calls) == 2


def test_order_title_skips_when_zapier_not_configured(insert_transaction):
    tx_id = insert_transaction
    stub = StubZapier(configured=False)
    result = order_title_through_arive(tx_id, client=stub)

    assert result["ok"] is False
    assert result["status"] == "skipped:zapier_mcp_not_configured"
    assert stub.calls == []  # no execute call attempted

    # Audit row still recorded so the operator knows the intent.
    with get_conn() as conn:
        rows = fetchall(
            conn,
            "SELECT * FROM tx_outbound_messages WHERE transaction_id = ?",
            (tx_id,),
        )
    assert len(rows) == 1
    assert rows[0]["reason"] == "title_ordered_via_arive"
    assert rows[0]["error"] == "zapier_mcp_not_configured"


def test_order_title_handles_zapier_exception(insert_transaction):
    tx_id = insert_transaction
    stub = StubZapier(raise_exc=RuntimeError("boom"))
    result = order_title_through_arive(tx_id, client=stub)

    assert result["ok"] is False
    assert "failed:" in result["status"]


def test_order_title_returns_failure_for_missing_tx(db_path):
    result = order_title_through_arive("tx_does_not_exist", client=StubZapier())
    assert result["ok"] is False
    assert result["status"] == "failed:transaction_not_found"
