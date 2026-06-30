"""
Tests for tx_coordinator.timeline.

The most important assertion is the Day-0 fix: timelines must anchor to
PSATerms.psa_execution_date, not date.today(). The original implementation
silently misdated every contingency for any PSA opened more than a day
after it was signed.
"""

from __future__ import annotations

from datetime import date, timedelta

from shared.schemas import PSATerms
from tx_coordinator.timeline import (
    days_to_close,
    generate_timeline,
    milestone_status_summary,
)


def _psa(**overrides) -> PSATerms:
    base = dict(
        purchase_price=95000.0,
        closing_date="2026-06-13",
        psa_execution_date="2026-05-14",
        inspection_period_days=10,
        financing_contingency_days=21,
        title_contingency_days=14,
        buyer_name="Buyer",
        seller_name="Seller",
        property_address="4521 Oak Ln",
    )
    base.update(overrides)
    return PSATerms(**base)


def test_day_zero_honors_psa_execution_date():
    """
    psa_execution_date = 2026-05-14, inspection_period_days = 10
    → inspection_response_deadline must land on 2026-05-24, regardless of
      when this test runs.
    """
    timeline = generate_timeline(_psa())
    by_name = {m["name"]: m for m in timeline}

    assert by_name["psa_executed"]["target_date"] == "2026-05-14"
    assert by_name["inspection_response_deadline"]["target_date"] == "2026-05-24"
    assert by_name["financing_contingency_deadline"]["target_date"] == "2026-06-04"
    assert by_name["title_contingency_deadline"]["target_date"] == "2026-05-28"
    assert by_name["closing_day"]["target_date"] == "2026-06-13"


def test_falls_back_to_today_when_psa_execution_date_missing():
    """
    Without psa_execution_date, generate_timeline uses today — old behavior.
    Day-0 must equal today, not crash.
    """
    psa = _psa(psa_execution_date=None,
               closing_date=(date.today() + timedelta(days=30)).isoformat())
    timeline = generate_timeline(psa)
    by_name = {m["name"]: m for m in timeline}
    assert by_name["psa_executed"]["target_date"] == date.today().isoformat()


def test_contingency_offsets_are_psa_driven():
    """If the PSA says 7-day inspection, the deadline lands on day 7, not day 10."""
    psa = _psa(inspection_period_days=7,
               financing_contingency_days=30,
               title_contingency_days=21)
    timeline = generate_timeline(psa)
    by_name = {m["name"]: m for m in timeline}
    assert by_name["inspection_response_deadline"]["target_date"] == "2026-05-21"
    assert by_name["financing_contingency_deadline"]["target_date"] == "2026-06-13"
    assert by_name["title_contingency_deadline"]["target_date"] == "2026-06-04"


def test_closing_day_always_matches_closing_date():
    psa = _psa(closing_date="2026-07-01")
    timeline = generate_timeline(psa)
    closing = next(m for m in timeline if m["name"] == "closing_day")
    assert closing["target_date"] == "2026-07-01"


def test_contingency_flags_present():
    timeline = generate_timeline(_psa())
    contingencies = [m for m in timeline if m.get("is_contingency")]
    types = {m["contingency_type"] for m in contingencies}
    assert types == {"inspection", "financing", "title"}


def test_milestone_status_summary_counts():
    timeline = generate_timeline(_psa())
    for m in timeline[:3]:
        m["status"] = "completed"
    summary = milestone_status_summary(timeline)
    assert summary["total"] == 16
    assert summary["completed"] == 3
    assert summary["pending"] == 13
    assert summary["completion_pct"] == 18.8


def test_days_to_close_handles_past_date():
    assert days_to_close("2000-01-01") < 0


def test_short_timeline_scales_generic_milestones():
    """A 15-day close should compress generic milestones proportionally."""
    psa = _psa(closing_date="2026-05-29")  # 15 days from psa_execution_date
    timeline = generate_timeline(psa)
    by_name = {m["name"]: m for m in timeline}
    # Generic earnest_money_deposited (day_offset=2 on 30-day template)
    # → 2 * 15 / 30 = 1 → 2026-05-15.
    assert by_name["earnest_money_deposited"]["target_date"] == "2026-05-15"
