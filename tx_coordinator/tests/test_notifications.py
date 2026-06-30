"""
Tests for tx_coordinator.notifications.

Covers the fan-out building block (notify_deal), the allowlist auto-fire
behavior (maybe_notify_on_completion), and the channel split: Arive email
inside the loan file + casual investor iMessage.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from shared.db import get_conn, fetchall, fetchone
from tx_coordinator import notifications
from tx_coordinator.notifications import (
    NOTIFY_ON_COMPLETE_ALLOWLIST,
    maybe_notify_on_completion,
    notify_deal,
)


# ── Stubs ─────────────────────────────────────────────────────────────────────


class StubZapier:
    def __init__(self, configured=True, response=None):
        self.configured = configured
        self.calls: list[dict] = []
        self._response = response or {
            "content": [{"text": '{"email_message_id": "EM-1001"}'}]
        }

    def execute(self, *, app, action, mode, params, instructions, output):
        self.calls.append({"app": app, "action": action, "params": params})
        return self._response


class StubOutbound:
    def __init__(self):
        self.calls: list[dict] = []

    def trigger_nurture(self, *, user_id, phone, context):
        self.calls.append({"phone": phone, "context": context})
        return {"id": f"stub-{len(self.calls)}", "ok": True}


def _set_arive_loan(tx_id: str, loan_id: str = "loan_AR_001") -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE transactions SET arive_loan_id = ? WHERE id = ?",
            (loan_id, tx_id),
        )
        conn.commit()


# ── notify_deal ───────────────────────────────────────────────────────────────


def test_notify_deal_posts_to_arive_and_texts_investor(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    zap = StubZapier()
    out = StubOutbound()

    result = notify_deal(
        tx_id,
        event_summary="the repair request was accepted",
        formal_subject="Repair request accepted",
        formal_body="Team — seller accepted the repair request. Details inside.",
        zapier_client=zap,
        outbound_client=out,
    )

    assert result["ok"] is True
    assert result["arive"]["status"] == "sent"
    assert result["investor"]["status"] == "sent"
    assert len(zap.calls) == 1
    assert zap.calls[0]["action"] == "send_loan_email"
    assert zap.calls[0]["params"]["loanId"] == "loan_AR_001"
    assert len(out.calls) == 1
    assert "just updated everyone" in out.calls[0]["context"]
    assert "the repair request was accepted" in out.calls[0]["context"]


def test_notify_deal_audits_both_channels(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)

    notify_deal(
        tx_id,
        event_summary="test event",
        formal_subject="s", formal_body="b",
        zapier_client=StubZapier(),
        outbound_client=StubOutbound(),
    )

    with get_conn() as conn:
        rows = fetchall(
            conn,
            "SELECT target_role, channel, reason, mode FROM tx_outbound_messages WHERE transaction_id = ? ORDER BY id",
            (tx_id,),
        )
    reasons = {(r["target_role"], r["channel"], r["reason"]) for r in rows}
    assert ("arive", "arive", "loan_file_email") in reasons
    assert ("investor", "imessage", "deal_update_investor_heads_up") in reasons


def test_notify_deal_uses_custom_investor_text(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    out = StubOutbound()

    notify_deal(
        tx_id,
        event_summary="title commitment landed",
        formal_subject="s", formal_body="b",
        investor_text="Custom heads-up for the investor",
        zapier_client=StubZapier(),
        outbound_client=out,
    )
    assert out.calls[0]["context"] == "Custom heads-up for the investor"


def test_notify_deal_without_zapier_configured_still_audits(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)

    result = notify_deal(
        tx_id,
        event_summary="x", formal_subject="s", formal_body="b",
        zapier_client=StubZapier(configured=False),
        outbound_client=StubOutbound(),
    )
    assert result["arive"]["status"] == "skipped:zapier_mcp_not_configured"
    # Overall ok stays True because the "skipped" path is benign in dev.
    assert result["ok"] is True


def test_notify_deal_skips_investor_when_buyer_has_no_phone(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    with get_conn() as conn:
        conn.execute(
            "UPDATE tx_parties SET phone = '' WHERE transaction_id = ? AND party_type = 'buyer'",
            (tx_id,),
        )
        conn.commit()

    result = notify_deal(
        tx_id,
        event_summary="x", formal_subject="s", formal_body="b",
        zapier_client=StubZapier(),
        outbound_client=StubOutbound(),
    )
    assert result["investor"]["status"] == "skipped:no_buyer_phone"


# ── maybe_notify_on_completion ────────────────────────────────────────────────


def test_allowlist_contains_the_5_critical_milestones():
    assert NOTIFY_ON_COMPLETE_ALLOWLIST == {
        "title_commitment_received",
        "clear_to_close",
        "closing_disclosure_received",
        "final_walkthrough",
        "closing_day",
    }


def test_maybe_notify_returns_none_for_non_allowlisted(insert_transaction):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    assert maybe_notify_on_completion(tx_id, "earnest_money_deposited") is None
    assert maybe_notify_on_completion(tx_id, "inspection_scheduled") is None


def test_maybe_notify_fires_for_clear_to_close(insert_transaction, monkeypatch):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)

    zap = StubZapier()
    out = StubOutbound()
    # The helper instantiates default clients internally; route them to stubs
    # by patching at module level.
    # post_loan_update instantiates ZapierMCPClient inside arive_actions;
    # _notify_investor instantiates OutboundClient inside notifications.
    from tx_coordinator import arive_actions
    monkeypatch.setattr(arive_actions, "ZapierMCPClient", lambda *a, **k: zap)
    monkeypatch.setattr(notifications, "OutboundClient", lambda *a, **k: out)

    result = maybe_notify_on_completion(tx_id, "clear_to_close")
    assert result is not None
    assert "Clear to Close" in result["arive"]["status"] or result["arive"]["ok"] is True
    assert len(zap.calls) == 1
    assert "Clear to Close" in zap.calls[0]["params"]["subject"]
    assert "4521 Oak Ln" in zap.calls[0]["params"]["body"]
    assert len(out.calls) == 1
    assert "Clear to Close" in out.calls[0]["context"]


def test_maybe_notify_fires_for_closing_day(insert_transaction, monkeypatch):
    tx_id = insert_transaction
    _set_arive_loan(tx_id)
    zap = StubZapier()
    out = StubOutbound()
    # post_loan_update instantiates ZapierMCPClient inside arive_actions;
    # _notify_investor instantiates OutboundClient inside notifications.
    from tx_coordinator import arive_actions
    monkeypatch.setattr(arive_actions, "ZapierMCPClient", lambda *a, **k: zap)
    monkeypatch.setattr(notifications, "OutboundClient", lambda *a, **k: out)

    result = maybe_notify_on_completion(tx_id, "closing_day")
    assert result is not None
    assert zap.calls[0]["params"]["subject"].startswith("Closed")
    assert "closed today" in zap.calls[0]["params"]["body"]
