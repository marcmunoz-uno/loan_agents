"""
Tests for tx_coordinator.sweeper.

Network is mocked: OutboundClient is replaced with a stub that records every
dispatch attempt so we can assert on rule output, channel routing, and
cooldown behavior without hitting tranchi-outbound-agent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from shared.db import get_conn
from tx_coordinator import guardrails
from tx_coordinator.sweeper import build_escalations, run_sweep


def _opt_in_live(tx_id: str):
    """Per-deal opt-in: flip a single deal live (global live alone won't send)."""
    with get_conn() as conn:
        conn.execute("UPDATE transactions SET agent_mode = 'live' WHERE id = ?", (tx_id,))
        conn.commit()


class StubOutbound:
    """OutboundClient replacement that records calls and returns canned refs."""

    def __init__(self):
        self.calls: list[dict] = []

    def trigger_nurture(self, *, user_id, phone, context):
        self.calls.append({"kind": "nurture", "phone": phone, "context": context})
        return {"id": f"stub-nurture-{len(self.calls)}", "ok": True}

    def trigger_voice_call(self, *, user_id, phone, owner_name="", property_address="", context=""):
        self.calls.append({"kind": "voice", "phone": phone, "owner_name": owner_name, "context": context})
        return {"id": f"stub-voice-{len(self.calls)}", "ok": True}


def _make_overdue(tx_id: str, contingency: str):
    past = (date.today() - timedelta(days=3)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tx_deadlines (transaction_id, contingency_type, deadline_date, status)
               VALUES (?, ?, ?, 'active')""",
            (tx_id, contingency, past),
        )
        conn.commit()


def test_build_escalations_emits_overdue_for_contingency(insert_transaction):
    tx_id = insert_transaction
    _make_overdue(tx_id, "inspection")

    escalations = build_escalations(tx_id)
    reasons = {e.reason for e in escalations}
    assert "inspection_contingency_overdue" in reasons


def test_sweep_in_shadow_does_not_dispatch(insert_transaction):
    tx_id = insert_transaction
    _make_overdue(tx_id, "inspection")

    stub = StubOutbound()
    summary = run_sweep(mode="shadow", client=stub)

    assert summary["mode"] == "shadow"
    assert summary["actions_logged_shadow"] >= 1
    assert summary["actions_sent_live"] == 0
    assert stub.calls == []  # no network in shadow mode


def test_global_live_without_opt_in_does_not_dispatch(insert_transaction):
    """Per-deal opt-in: global live alone must NOT send on an un-opted deal."""
    tx_id = insert_transaction
    _make_overdue(tx_id, "inspection")

    stub = StubOutbound()
    summary = run_sweep(mode="live", client=stub)

    assert summary["actions_sent_live"] == 0
    assert stub.calls == []


def test_sweep_in_live_dispatches_through_outbound(insert_transaction, monkeypatch):
    tx_id = insert_transaction
    _make_overdue(tx_id, "inspection")
    _opt_in_live(tx_id)
    # Make the send time-of-day independent so the test isn't flaky.
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: True)

    stub = StubOutbound()
    summary = run_sweep(mode="live", client=stub)

    assert summary["mode"] == "live"
    assert summary["actions_sent_live"] >= 1
    assert any(c["kind"] == "nurture" for c in stub.calls)


def test_sweep_cooldown_prevents_repeat_within_window(insert_transaction):
    tx_id = insert_transaction
    _make_overdue(tx_id, "inspection")
    stub = StubOutbound()

    first = run_sweep(mode="shadow", client=stub)
    second = run_sweep(mode="shadow", client=stub)

    assert first["actions_logged_shadow"] >= 1
    # Second sweep must hit cooldown for at least the inspection_contingency_overdue reason.
    assert second["actions_skipped_cooldown"] >= 1


def test_sweep_skips_closed_transactions(insert_transaction):
    tx_id = insert_transaction
    _make_overdue(tx_id, "inspection")
    with get_conn() as conn:
        conn.execute("UPDATE transactions SET status = 'closed' WHERE id = ?", (tx_id,))
        conn.commit()

    summary = run_sweep(mode="shadow", client=StubOutbound())
    assert summary["transactions_scanned"] == 0


def test_build_escalations_emits_arive_title_order_when_milestone_pending(insert_transaction):
    """
    The seed PSA was executed 2026-05-14; by the time this test runs (any date
    after that), the title_ordered milestone has been pending >=2 days and the
    sweeper should want to fire arive.order_title.
    """
    tx_id = insert_transaction
    escalations = build_escalations(tx_id)
    reasons = {e.reason for e in escalations}
    assert "title_ordered_via_arive" in reasons
    arive_esc = next(e for e in escalations if e.reason == "title_ordered_via_arive")
    assert arive_esc.channel == "arive"
    assert arive_esc.target_role == "arive"


def test_build_escalations_skips_arive_when_title_already_completed(insert_transaction):
    tx_id = insert_transaction
    with get_conn() as conn:
        conn.execute(
            """UPDATE tx_milestones SET status = 'completed'
               WHERE transaction_id = ? AND milestone_name = 'title_ordered'""",
            (tx_id,),
        )
        conn.commit()
    escalations = build_escalations(tx_id)
    reasons = {e.reason for e in escalations}
    assert "title_ordered_via_arive" not in reasons
