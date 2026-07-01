"""
tx_coordinator/deadline_engine.py — Contingency deadline tracking and alerting.

Tracks inspection, financing, title, and appraisal contingency deadlines.
Returns warning levels: none | approaching (3+ days) | urgent (1-2 days) | overdue
"""

from __future__ import annotations
from datetime import date, timedelta
from typing import Any, Literal, Optional

from shared.db import get_conn, fetchall, fetchone


WarningLevel = Literal["none", "approaching", "urgent", "overdue"]


def _days_until(deadline_date: str) -> int:
    """Days until a deadline. Negative = past due."""
    try:
        target = date.fromisoformat(deadline_date)
        return (target - date.today()).days
    except ValueError:
        return 0


def _compute_warning_level(days_remaining: int) -> WarningLevel:
    if days_remaining < 0:
        return "overdue"
    elif days_remaining <= 2:
        return "urgent"
    elif days_remaining <= 5:
        return "approaching"
    else:
        return "none"


# ── Public functions ──────────────────────────────────────────────────────────

def inspection_period_end(psa_date: str, period_days: int) -> str:
    """Return the ISO date when the inspection period ends."""
    start = date.fromisoformat(psa_date)
    return (start + timedelta(days=period_days)).isoformat()


def financing_contingency_end(psa_date: str, period_days: int) -> str:
    """Return the ISO date when the financing contingency expires."""
    start = date.fromisoformat(psa_date)
    return (start + timedelta(days=period_days)).isoformat()


def title_contingency_end(psa_date: str, period_days: int) -> str:
    """Return the ISO date when the title contingency expires."""
    start = date.fromisoformat(psa_date)
    return (start + timedelta(days=period_days)).isoformat()


def enrich_deadline(row: dict) -> dict:
    """Add days_remaining and warning_level to a deadline row."""
    days = _days_until(row["deadline_date"])
    level = _compute_warning_level(days)
    return {
        **row,
        "days_remaining": days,
        "warning_level": level,
    }


def next_critical_deadline(tx_id: str) -> Optional[dict[str, Any]]:
    """
    Return the soonest active deadline for a transaction, enriched with warning level.
    Returns None if no active deadlines exist.
    """
    with get_conn() as conn:
        rows = fetchall(
            conn,
            """
            SELECT * FROM tx_deadlines
            WHERE transaction_id = ? AND status = 'active'
            ORDER BY deadline_date ASC
            LIMIT 1
            """,
            (tx_id,)
        )
    if not rows:
        return None
    return enrich_deadline(rows[0])


def overdue_items(tx_id: str) -> list[dict[str, Any]]:
    """
    Return all active deadlines that are past their deadline date.
    """
    today = date.today().isoformat()
    with get_conn() as conn:
        rows = fetchall(
            conn,
            """
            SELECT * FROM tx_deadlines
            WHERE transaction_id = ? AND status = 'active' AND deadline_date < ?
            ORDER BY deadline_date ASC
            """,
            (tx_id, today)
        )
    return [enrich_deadline(r) for r in rows]


def all_deadlines(tx_id: str) -> list[dict[str, Any]]:
    """Return all deadlines for a transaction, enriched with current warning level."""
    with get_conn() as conn:
        rows = fetchall(
            conn,
            "SELECT * FROM tx_deadlines WHERE transaction_id = ? ORDER BY deadline_date ASC",
            (tx_id,)
        )
    return [enrich_deadline(r) for r in rows]


def upcoming_deadlines(tx_id: str, days_ahead: int = 7) -> list[dict[str, Any]]:
    """
    Return active deadlines within the next N days (default 7).
    Sorted by urgency (soonest first).
    """
    cutoff = (date.today() + timedelta(days=days_ahead)).isoformat()
    today = date.today().isoformat()
    with get_conn() as conn:
        rows = fetchall(
            conn,
            """
            SELECT * FROM tx_deadlines
            WHERE transaction_id = ?
              AND status = 'active'
              AND deadline_date BETWEEN ? AND ?
            ORDER BY deadline_date ASC
            """,
            (tx_id, today, cutoff)
        )
    return [enrich_deadline(r) for r in rows]


def resolve_deadline(tx_id: str, contingency_type: str, actor: str = "system") -> bool:
    """Mark a deadline as resolved. Returns True if a row was updated."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE tx_deadlines
            SET status = 'resolved', resolved_at = ?
            WHERE transaction_id = ? AND contingency_type = ? AND status = 'active'
            """,
            (now, tx_id, contingency_type)
        )
        conn.commit()
        return cur.rowcount > 0


def deadline_health_check(tx_id: str) -> dict[str, Any]:
    """
    Return a health summary for a transaction's contingency deadlines.

    Used for the GET /api/tx/:id endpoint to surface risk at a glance.
    """
    deadlines = all_deadlines(tx_id)
    active = [d for d in deadlines if d["status"] == "active"]

    overdue = [d for d in active if d["warning_level"] == "overdue"]
    urgent = [d for d in active if d["warning_level"] == "urgent"]
    approaching = [d for d in active if d["warning_level"] == "approaching"]

    if overdue:
        overall_health = "critical"
    elif urgent:
        overall_health = "at_risk"
    elif approaching:
        overall_health = "watch"
    else:
        overall_health = "healthy"

    return {
        "health": overall_health,
        "active_deadlines": len(active),
        "overdue": overdue,
        "urgent": urgent,
        "approaching": approaching,
        "all": deadlines,
    }
