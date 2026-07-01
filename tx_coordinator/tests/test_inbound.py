"""
Tests for inbound reply handling. The LLM interpreter is injected so these are
deterministic and offline; state changes are asserted directly against the DB.
"""

from __future__ import annotations

import pytest

from tx_coordinator import guardrails
from tx_coordinator.inbound import handle_inbound_reply, normalize_phone
from shared.db import get_conn, fetchone, fetchall

BUYER_PHONE = "+13135550100"  # matches conftest.sample_psa buyer_phone


class FakeClient:
    def __init__(self):
        self.calls = []

    def trigger_nurture(self, *, user_id, phone, context):
        self.calls.append((user_id, phone, context))
        return {"id": f"reply_{len(self.calls)}"}


def _stub(**over):
    base = {"intent": "provide_info", "contingency_type": None, "milestone_name": None,
            "confidence": 0.9, "summary": "buyer said something", "reply": "Got it."}
    base.update(over)
    return lambda text, ctx, last: base


def _set_live(tx_id):
    with get_conn() as conn:
        conn.execute("UPDATE transactions SET agent_mode = 'live' WHERE id = ?", (tx_id,))
        conn.commit()


def _deadline_status(tx_id, ct):
    with get_conn() as conn:
        row = fetchone(conn,
            "SELECT status FROM tx_deadlines WHERE transaction_id = ? AND contingency_type = ?",
            (tx_id, ct))
    return (row or {}).get("status")


def _milestone_status(tx_id, name):
    with get_conn() as conn:
        row = fetchone(conn,
            "SELECT status FROM tx_milestones WHERE transaction_id = ? AND milestone_name = ?",
            (tx_id, name))
    return (row or {}).get("status")


def _inbound_comms(tx_id):
    with get_conn() as conn:
        return fetchall(conn,
            "SELECT * FROM tx_communications WHERE transaction_id = ? AND direction = 'in'", (tx_id,))


# ── matching ──────────────────────────────────────────────────────────────────


def test_normalize_phone():
    assert normalize_phone("+1 (313) 555-0100") == "3135550100"
    assert normalize_phone("313.555.0100") == "3135550100"


def test_unmatched_sender(db_path, insert_transaction):
    res = handle_inbound_reply("+19998887777", "hello", interpret=_stub())
    assert res["ok"] is False
    assert res["status"] == "unmatched_sender"


# ── remembering works in ANY mode ───────────────────────────────────────────────


def test_reply_always_logged_as_communication(db_path, insert_transaction):
    res = handle_inbound_reply(BUYER_PHONE, "fyi the appraiser comes friday",
                               interpret=_stub(intent="provide_info", summary="appraisal fri"))
    assert res["ok"] is True
    comms = _inbound_comms(insert_transaction)
    assert len(comms) == 1
    assert "fyi the appraiser" in comms[0]["full_text"]


def test_resolve_contingency_stops_the_nudge(db_path, insert_transaction):
    assert _deadline_status(insert_transaction, "inspection") == "active"
    res = handle_inbound_reply(BUYER_PHONE, "already waived the inspection",
                               interpret=_stub(intent="resolve_contingency",
                                               contingency_type="inspection", confidence=0.95))
    assert res["applied"]["action"] == "resolved_contingency"
    assert _deadline_status(insert_transaction, "inspection") == "resolved"


def test_confirm_buyer_actionable_milestone_completes(db_path, insert_transaction):
    res = handle_inbound_reply(BUYER_PHONE, "ordered title yesterday",
                               interpret=_stub(intent="confirm_milestone",
                                               milestone_name="title_ordered", confidence=0.9))
    assert res["applied"]["action"] == "completed_milestone"
    assert _milestone_status(insert_transaction, "title_ordered") == "completed"


def test_high_stakes_milestone_is_flagged_not_completed(db_path, insert_transaction):
    res = handle_inbound_reply(BUYER_PHONE, "we're clear to close right?",
                               interpret=_stub(intent="confirm_milestone",
                                               milestone_name="clear_to_close", confidence=0.9))
    assert res["applied"]["action"] == "flagged_for_human"
    assert _milestone_status(insert_transaction, "clear_to_close") != "completed"


def test_low_confidence_asks_to_clarify_and_changes_nothing(db_path, insert_transaction):
    res = handle_inbound_reply(BUYER_PHONE, "yeah that thing",
                               interpret=_stub(intent="resolve_contingency",
                                               contingency_type="inspection", confidence=0.3))
    assert res["applied"]["action"] == "clarify"
    assert _deadline_status(insert_transaction, "inspection") == "active"  # untouched


# ── reply gating ────────────────────────────────────────────────────────────────


def test_reply_not_sent_in_shadow_but_state_still_applied(db_path, insert_transaction):
    client = FakeClient()
    res = handle_inbound_reply(BUYER_PHONE, "waived inspection",
                               interpret=_stub(intent="resolve_contingency",
                                               contingency_type="inspection", confidence=0.9),
                               client=client)
    assert res["reply"]["sent"] is False
    assert res["reply"]["reason"] == "shadow"
    assert client.calls == []
    assert _deadline_status(insert_transaction, "inspection") == "resolved"  # remembered anyway


def test_reply_sent_when_deal_is_live(db_path, insert_transaction, monkeypatch):
    monkeypatch.setenv("TX_AGENT_MODE", "live")
    _set_live(insert_transaction)
    client = FakeClient()
    res = handle_inbound_reply(BUYER_PHONE, "waived it",
                               interpret=_stub(intent="resolve_contingency",
                                               contingency_type="inspection", confidence=0.9),
                               client=client)
    assert res["reply"]["sent"] is True
    assert len(client.calls) == 1


def test_kill_switch_blocks_reply_but_not_memory(db_path, insert_transaction, monkeypatch):
    monkeypatch.setenv("TX_AGENT_MODE", "live")
    _set_live(insert_transaction)
    guardrails.set_kill_switch(True)
    client = FakeClient()
    res = handle_inbound_reply(BUYER_PHONE, "waived it",
                               interpret=_stub(intent="resolve_contingency",
                                               contingency_type="inspection", confidence=0.9),
                               client=client)
    assert res["reply"]["sent"] is False
    assert res["reply"]["reason"] == "kill_switch"
    assert client.calls == []
    assert _deadline_status(insert_transaction, "inspection") == "resolved"


# ── HTTP webhook (real interpreter → offline stub path, no API key) ─────────────


def test_webhook_rejects_bad_secret(client, insert_transaction):
    resp = client.post("/api/tx/webhook/inbound?secret=wrong",
                       json={"from_phone": BUYER_PHONE, "text": "hi"})
    assert resp.status_code == 401


def test_webhook_missing_fields_400(client):
    from .conftest import TEST_SECRET
    resp = client.post(f"/api/tx/webhook/inbound?secret={TEST_SECRET}", json={"text": "hi"})
    assert resp.status_code == 400


def test_webhook_happy_path_matches_and_logs(client, insert_transaction, monkeypatch):
    from .conftest import TEST_SECRET
    # Stub the LLM so this exercises the real route → interpret → parse → apply
    # path deterministically (independent of whether an API key is present).
    canned = ('{"intent":"resolve_contingency","contingency_type":"inspection",'
              '"milestone_name":null,"confidence":0.95,"summary":"buyer waived inspection",'
              '"reply":"Got it — noted you waived inspection."}')
    monkeypatch.setattr("tx_coordinator.inbound.chat", lambda **kw: canned)

    resp = client.post(f"/api/tx/webhook/inbound?secret={TEST_SECRET}",
                       json={"from_phone": BUYER_PHONE, "text": "already waived inspection"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["tx_id"] == insert_transaction
    assert data["applied"]["action"] == "resolved_contingency"
    assert _deadline_status(insert_transaction, "inspection") == "resolved"
    assert len(_inbound_comms(insert_transaction)) == 1
