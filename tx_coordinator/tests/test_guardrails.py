"""
Tests for live-mode guardrails: per-deal opt-in, kill switch, channel allowlist,
quiet hours, and the rolling daily send cap.

Sweeper-integration tests monkeypatch build_escalations to a controlled set so
they don't depend on the wall clock or on Zapier/Arive being configured, and
force within_quiet_hours=True so they aren't flaky by time of day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tx_coordinator import guardrails, sweeper
from tx_coordinator.sweeper import Escalation, run_sweep
from shared.db import get_conn

UTC = timezone.utc


# ── helpers ───────────────────────────────────────────────────────────────────


class FakeClient:
    """Records outbound calls instead of hitting tranchi-outbound-agent."""

    def __init__(self):
        self.calls = []

    def trigger_nurture(self, *, user_id, phone, context):
        self.calls.append(("nurture", user_id, phone, context))
        return {"id": f"msg_{len(self.calls)}"}

    def trigger_voice_call(self, *, user_id, phone, owner_name="", property_address="", **kw):
        self.calls.append(("voice", user_id, phone))
        return {"id": f"call_{len(self.calls)}"}


def _esc(tx_id, reason, channel="imessage", role="investor"):
    return Escalation(
        transaction_id=tx_id, reason=reason, target_role=role,
        party_id=None, channel=channel, body=f"body for {reason}",
    )


def _set_live(tx_id):
    with get_conn() as conn:
        conn.execute("UPDATE transactions SET agent_mode = 'live' WHERE id = ?", (tx_id,))
        conn.commit()


def _insert_live_send(tx_id, reason, *, when, error=""):
    """Drop a row straight into the audit ledger to simulate prior live sends."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tx_outbound_messages
               (transaction_id, party_id, target_role, channel, reason, body,
                mode, outbound_ref, sent_at, error)
               VALUES (?, NULL, 'investor', 'imessage', ?, 'b', 'live', 'ref', ?, ?)""",
            (tx_id, reason, when.isoformat(), error),
        )
        conn.commit()


# ── unit: individual gates ──────────────────────────────────────────────────────


def test_channel_allowlist(db_path):
    assert guardrails.channel_allowed("imessage")
    assert guardrails.channel_allowed("email")
    assert guardrails.channel_allowed("arive")
    assert not guardrails.channel_allowed("voice")  # held back


def test_quiet_hours_window(db_path):
    # 14:00 UTC = 10:00 EDT → inside 8–20 window
    assert guardrails.within_quiet_hours(datetime(2026, 6, 30, 14, 0, tzinfo=UTC))
    # 07:00 UTC = 03:00 EDT → outside
    assert not guardrails.within_quiet_hours(datetime(2026, 6, 30, 7, 0, tzinfo=UTC))


def test_kill_switch_roundtrip(db_path):
    assert guardrails.kill_switch_active() is False
    guardrails.set_kill_switch(True)
    assert guardrails.kill_switch_active() is True
    guardrails.set_kill_switch(False)
    assert guardrails.kill_switch_active() is False


def test_daily_cap_counts_only_recent_successful(db_path, insert_transaction):
    now = datetime(2026, 6, 30, 14, 0, tzinfo=UTC)
    _insert_live_send(insert_transaction, "r1", when=now - timedelta(hours=1))
    _insert_live_send(insert_transaction, "r2", when=now - timedelta(hours=2))
    _insert_live_send(insert_transaction, "old", when=now - timedelta(hours=30))   # outside 24h
    _insert_live_send(insert_transaction, "failed", when=now - timedelta(hours=1), error="boom")  # not counted
    assert guardrails.live_sends_last_24h(now) == 2


def test_evaluate_order(db_path):
    now = datetime(2026, 6, 30, 14, 0, tzinfo=UTC)  # in-window
    # kill switch wins over everything
    guardrails.set_kill_switch(True)
    assert guardrails.evaluate("imessage", now) == "kill_switch"
    guardrails.set_kill_switch(False)
    # disabled channel
    assert guardrails.evaluate("voice", now) == "channel_disabled:voice"
    # quiet hours
    assert guardrails.evaluate("imessage", datetime(2026, 6, 30, 7, 0, tzinfo=UTC)) == "quiet_hours"
    # all clear
    assert guardrails.evaluate("imessage", now) is None


# ── integration: per-deal opt-in ────────────────────────────────────────────────


def test_global_live_but_deal_not_opted_in_stays_shadow(db_path, insert_transaction, monkeypatch):
    monkeypatch.setattr(sweeper, "build_escalations",
                        lambda tx_id: [_esc(tx_id, "inspection_overdue")])
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: True)
    client = FakeClient()

    # global live, but the deal was never flipped live → no send
    summary = run_sweep(mode="live", client=client)

    assert client.calls == []
    assert summary["actions_sent_live"] == 0
    assert summary["actions_logged_shadow"] == 1


def test_opted_in_deal_sends_live_then_cooldown(db_path, insert_transaction, monkeypatch):
    monkeypatch.setattr(sweeper, "build_escalations",
                        lambda tx_id: [_esc(tx_id, "inspection_overdue")])
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: True)
    _set_live(insert_transaction)
    client = FakeClient()

    first = run_sweep(mode="live", client=client)
    assert first["actions_sent_live"] == 1
    assert len(client.calls) == 1

    # second sweep: same reason is now on cooldown → no second send
    second = run_sweep(mode="live", client=client)
    assert second["actions_sent_live"] == 0
    assert len(client.calls) == 1
    assert any(s["cause"] == "cooldown" for s in second["skipped"])


# ── integration: each guardrail blocks a live-opted deal ────────────────────────


def test_cap_blocks_beyond_limit(db_path, insert_transaction, monkeypatch):
    # cap default is 3 → 4 distinct escalations, only 3 should send
    escs = [_esc(insert_transaction, f"reason_{i}") for i in range(4)]
    monkeypatch.setattr(sweeper, "build_escalations", lambda tx_id: escs)
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: True)
    _set_live(insert_transaction)
    client = FakeClient()

    summary = run_sweep(mode="live", client=client)

    assert summary["actions_sent_live"] == 3
    assert len(client.calls) == 3
    assert sum(1 for s in summary["skipped"] if s["cause"] == "cap_reached") == 1


def test_voice_channel_blocked(db_path, insert_transaction, monkeypatch):
    monkeypatch.setattr(sweeper, "build_escalations",
                        lambda tx_id: [_esc(tx_id, "lender_silent", channel="voice", role="lender")])
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: True)
    _set_live(insert_transaction)
    client = FakeClient()

    summary = run_sweep(mode="live", client=client)

    assert client.calls == []
    assert summary["actions_sent_live"] == 0
    assert any(s["cause"] == "channel_disabled:voice" for s in summary["skipped"])


def test_kill_switch_blocks_all_live(db_path, insert_transaction, monkeypatch):
    monkeypatch.setattr(sweeper, "build_escalations",
                        lambda tx_id: [_esc(tx_id, "inspection_overdue")])
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: True)
    _set_live(insert_transaction)
    guardrails.set_kill_switch(True)
    client = FakeClient()

    summary = run_sweep(mode="live", client=client)

    assert client.calls == []
    assert summary["actions_sent_live"] == 0
    assert any(s["cause"] == "kill_switch" for s in summary["skipped"])


def test_quiet_hours_blocks_without_consuming(db_path, insert_transaction, monkeypatch):
    monkeypatch.setattr(sweeper, "build_escalations",
                        lambda tx_id: [_esc(tx_id, "inspection_overdue")])
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: False)  # outside window
    _set_live(insert_transaction)
    client = FakeClient()

    summary = run_sweep(mode="live", client=client)

    assert client.calls == []
    assert any(s["cause"] == "quiet_hours" for s in summary["skipped"])
    # blocked send wrote NO audit row → nothing to consume the cap
    assert guardrails.live_sends_last_24h() == 0


# ── cooldown is mode/error-aware (the review fixes) ──────────────────────────────


class FailingClient(FakeClient):
    def trigger_nurture(self, *, user_id, phone, context):
        self.calls.append(("nurture", user_id, phone, context))
        return {"error": "boom"}


def test_failed_live_dispatch_retries_next_sweep(db_path, insert_transaction, monkeypatch):
    """A failed live send must NOT lock the escalation out for 24h."""
    monkeypatch.setattr(sweeper, "build_escalations",
                        lambda tx_id: [_esc(tx_id, "inspection_overdue")])
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: True)
    _set_live(insert_transaction)
    client = FailingClient()

    first = run_sweep(mode="live", client=client)
    assert first["actions_sent_live"] == 0
    assert len(first["errors"]) == 1
    # a failed send doesn't count toward the cap
    assert guardrails.live_sends_last_24h() == 0

    # next sweep retries instead of treating it as on-cooldown
    second = run_sweep(mode="live", client=client)
    assert len(client.calls) == 2
    assert not any(s["cause"] == "cooldown" for s in second["skipped"])


def test_shadow_rows_do_not_block_first_live_send(db_path, insert_transaction, monkeypatch):
    """Flipping a deal live should send immediately, not wait out shadow cooldown."""
    monkeypatch.setattr(sweeper, "build_escalations",
                        lambda tx_id: [_esc(tx_id, "inspection_overdue")])
    monkeypatch.setattr(guardrails, "within_quiet_hours", lambda now=None: True)

    # accrue a shadow row for this reason
    shadow = run_sweep(mode="shadow", client=FakeClient())
    assert shadow["actions_logged_shadow"] == 1

    # now opt the deal live — the prior shadow row must NOT suppress the live send
    _set_live(insert_transaction)
    client = FakeClient()
    live = run_sweep(mode="live", client=client)
    assert live["actions_sent_live"] == 1
    assert len(client.calls) == 1


# ── go-live respects deal status (HTTP) ─────────────────────────────────────────


def test_go_live_on_open_deal(client, auth_headers, insert_transaction):
    resp = client.post(f"/api/tx/{insert_transaction}/go-live", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()["agent_mode"] == "live"


def test_go_live_on_missing_deal_404(client, auth_headers):
    resp = client.post("/api/tx/tx_missing/go-live", headers=auth_headers)
    assert resp.status_code == 404


def test_go_live_on_closed_deal_409(client, auth_headers, insert_transaction):
    with get_conn() as conn:
        conn.execute("UPDATE transactions SET status = 'closed' WHERE id = ?", (insert_transaction,))
        conn.commit()
    resp = client.post(f"/api/tx/{insert_transaction}/go-live", headers=auth_headers)
    assert resp.status_code == 409
