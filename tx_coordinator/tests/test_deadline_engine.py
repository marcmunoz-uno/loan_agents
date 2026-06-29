"""
Tests for tx_coordinator.deadline_engine.

Warning-level transitions and the health rollup are the contract the sweeper
depends on — these tests pin that contract.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from shared.db import get_conn
from tx_coordinator.deadline_engine import (
    _compute_warning_level,
    all_deadlines,
    deadline_health_check,
    overdue_items,
    resolve_deadline,
    upcoming_deadlines,
)


@pytest.mark.parametrize("days, expected", [
    (-5, "overdue"),
    (-1, "overdue"),
    (0, "urgent"),
    (1, "urgent"),
    (2, "urgent"),
    (3, "approaching"),
    (5, "approaching"),
    (6, "none"),
    (30, "none"),
])
def test_warning_levels(days, expected):
    assert _compute_warning_level(days) == expected


def _seed_deadline(tx_id: str, contingency_type: str, days_offset: int):
    """Insert a deadline N days from today."""
    deadline_date = (date.today() + timedelta(days=days_offset)).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tx_deadlines (transaction_id, contingency_type, deadline_date, status)
               VALUES (?, ?, ?, 'active')""",
            (tx_id, contingency_type, deadline_date),
        )
        conn.commit()


def test_health_rollup_critical_when_overdue(insert_transaction):
    tx_id = insert_transaction
    # Wipe the seeded deadlines so this test asserts in isolation.
    with get_conn() as conn:
        conn.execute("DELETE FROM tx_deadlines WHERE transaction_id = ?", (tx_id,))
        conn.commit()
    _seed_deadline(tx_id, "inspection", -3)
    health = deadline_health_check(tx_id)
    assert health["health"] == "critical"
    assert len(health["overdue"]) == 1


def test_health_rollup_at_risk_when_urgent(insert_transaction):
    tx_id = insert_transaction
    with get_conn() as conn:
        conn.execute("DELETE FROM tx_deadlines WHERE transaction_id = ?", (tx_id,))
        conn.commit()
    _seed_deadline(tx_id, "financing", 1)
    health = deadline_health_check(tx_id)
    assert health["health"] == "at_risk"
    assert len(health["urgent"]) == 1


def test_health_rollup_watch_when_approaching(insert_transaction):
    tx_id = insert_transaction
    with get_conn() as conn:
        conn.execute("DELETE FROM tx_deadlines WHERE transaction_id = ?", (tx_id,))
        conn.commit()
    _seed_deadline(tx_id, "title", 4)
    health = deadline_health_check(tx_id)
    assert health["health"] == "watch"
    assert len(health["approaching"]) == 1


def test_health_rollup_healthy_when_clear(insert_transaction):
    tx_id = insert_transaction
    with get_conn() as conn:
        conn.execute("DELETE FROM tx_deadlines WHERE transaction_id = ?", (tx_id,))
        conn.commit()
    _seed_deadline(tx_id, "inspection", 20)
    health = deadline_health_check(tx_id)
    assert health["health"] == "healthy"


def test_resolve_deadline_marks_resolved(insert_transaction):
    tx_id = insert_transaction
    assert resolve_deadline(tx_id, "inspection") is True
    # Second call must be a no-op (only 'active' rows update).
    assert resolve_deadline(tx_id, "inspection") is False

    remaining = [d for d in all_deadlines(tx_id) if d["status"] == "active"]
    types = {d["contingency_type"] for d in remaining}
    assert "inspection" not in types


def test_upcoming_deadlines_window(insert_transaction):
    tx_id = insert_transaction
    upcoming = upcoming_deadlines(tx_id, days_ahead=14)
    # Inspection is at day 10 from PSA execution (2026-05-24); whether it falls
    # inside the 14-day window depends on the test clock. Just assert the
    # window is correctly applied — every returned row must be within range.
    today = date.today()
    cutoff = today + timedelta(days=14)
    for d in upcoming:
        deadline = date.fromisoformat(d["deadline_date"])
        assert today <= deadline <= cutoff


def test_overdue_items_only_returns_past_deadlines(insert_transaction):
    tx_id = insert_transaction
    # Backdate inspection by 5 days
    yesterday = (date.today() - timedelta(days=5)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE tx_deadlines SET deadline_date = ? WHERE transaction_id = ? AND contingency_type = 'inspection'",
            (yesterday, tx_id),
        )
        conn.commit()
    items = overdue_items(tx_id)
    assert any(d["contingency_type"] == "inspection" for d in items)
    for d in items:
        assert d["warning_level"] == "overdue"
