"""
tx_coordinator/timeline.py — Milestone timeline generation.

Generates the full 30-day (or custom) milestone timeline from PSA terms.
Each milestone has a name, label, target date, and sequence order.
"""

from __future__ import annotations
from datetime import date, timedelta
from typing import Any

from shared.schemas import PSATerms


# ── Milestone definitions ─────────────────────────────────────────────────────
#
# Each entry: (slug, label, day_offset_from_psa, is_contingency_related)
# day_offset is relative to PSA date (day 0). Ranges use the midpoint as default;
# PSA terms override specific offsets where applicable.

MILESTONE_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "psa_executed",
        "label": "PSA Executed",
        "day_offset": 0,
        "sequence": 1,
        "description": "Purchase and Sale Agreement signed by all parties.",
    },
    {
        "name": "earnest_money_deposited",
        "label": "Earnest Money Deposited",
        "day_offset": 2,
        "sequence": 2,
        "description": "Earnest money delivered to escrow/title company.",
    },
    {
        "name": "title_ordered",
        "label": "Title Search Ordered",
        "day_offset": 2,
        "sequence": 3,
        "description": "Title company ordered the title search.",
    },
    {
        "name": "inspection_scheduled",
        "label": "Inspection Scheduled",
        "day_offset": 3,
        "sequence": 4,
        "description": "Home inspection appointment booked.",
    },
    {
        "name": "loan_application_submitted",
        "label": "Loan Application Submitted",
        "day_offset": 5,
        "sequence": 5,
        "description": "Buyer's loan application formally submitted to lender.",
    },
    {
        "name": "inspection_completed",
        "label": "Inspection Completed",
        "day_offset": 6,
        "sequence": 6,
        "description": "Property inspection conducted.",
    },
    {
        "name": "inspection_response_deadline",
        "label": "Inspection Response Deadline",
        "day_offset_key": "inspection_period_days",  # driven by PSA terms
        "day_offset": 10,
        "sequence": 7,
        "description": "Buyer must submit repair requests or waive inspection contingency.",
        "is_contingency": True,
        "contingency_type": "inspection",
    },
    {
        "name": "title_commitment_received",
        "label": "Title Commitment Received",
        "day_offset": 12,
        "sequence": 8,
        "description": "Title company issues preliminary title commitment.",
    },
    {
        "name": "appraisal_ordered",
        "label": "Appraisal Ordered",
        "day_offset": 10,
        "sequence": 9,
        "description": "Lender orders property appraisal.",
    },
    {
        "name": "appraisal_completed",
        "label": "Appraisal Completed",
        "day_offset": 18,
        "sequence": 10,
        "description": "Appraisal report received by lender.",
    },
    {
        "name": "title_contingency_deadline",
        "label": "Title Contingency Deadline",
        "day_offset_key": "title_contingency_days",
        "day_offset": 14,
        "sequence": 11,
        "description": "All title defects must be cured or buyer must object.",
        "is_contingency": True,
        "contingency_type": "title",
    },
    {
        "name": "financing_contingency_deadline",
        "label": "Financing Contingency Removed",
        "day_offset_key": "financing_contingency_days",
        "day_offset": 21,
        "sequence": 12,
        "description": "Buyer must have loan approval in hand or waive financing contingency.",
        "is_contingency": True,
        "contingency_type": "financing",
    },
    {
        "name": "clear_to_close",
        "label": "Clear to Close Received",
        "day_offset": 25,
        "sequence": 13,
        "description": "Lender issues CTC — final loan approval.",
    },
    {
        "name": "closing_disclosure_received",
        "label": "Closing Disclosure Received (CD)",
        "day_offset": 27,
        "sequence": 14,
        "description": "Buyer receives CD — required 3 business days before closing per RESPA.",
    },
    {
        "name": "final_walkthrough",
        "label": "Final Walk-Through",
        "day_offset": 28,
        "sequence": 15,
        "description": "Buyer confirms property condition before closing.",
    },
    {
        "name": "closing_day",
        "label": "Closing Day",
        "day_offset_key": "closing_day",  # special: computed from closing_date
        "day_offset": 30,
        "sequence": 16,
        "description": "Deed signed, funds wired, keys transferred.",
    },
]


def generate_timeline(psa_terms: PSATerms) -> list[dict[str, Any]]:
    """
    Generate the full milestone timeline from PSA terms.

    Day 0 is taken from `psa_terms.psa_execution_date` when present; otherwise
    we fall back to today. This matters because a PSA signed two days ago has
    its inspection contingency deadline anchored to that signing date, not to
    the moment this API was called.

    Returns a list of milestone dicts, each with:
        name, label, sequence, target_date (ISO), description, is_contingency (bool)
    """
    closing_date_obj = date.fromisoformat(psa_terms.closing_date)

    if psa_terms.psa_execution_date:
        day_0 = date.fromisoformat(psa_terms.psa_execution_date)
    else:
        day_0 = date.today()

    days_to_close = (closing_date_obj - day_0).days
    if days_to_close <= 0:
        days_to_close = 30

    milestones = []
    for tmpl in MILESTONE_TEMPLATES:
        # Determine day offset
        if tmpl["name"] == "closing_day":
            target = closing_date_obj
        elif "day_offset_key" in tmpl:
            key = tmpl["day_offset_key"]
            offset = getattr(psa_terms, key, None) or tmpl["day_offset"]
            target = day_0 + timedelta(days=int(offset))
        else:
            # Scale generic milestones proportionally if close is not 30 days
            raw_offset = tmpl["day_offset"]
            if days_to_close != 30:
                scaled = int(raw_offset * days_to_close / 30)
            else:
                scaled = raw_offset
            target = day_0 + timedelta(days=scaled)

        milestones.append({
            "name": tmpl["name"],
            "label": tmpl["label"],
            "sequence": tmpl["sequence"],
            "target_date": target.isoformat(),
            "description": tmpl["description"],
            "is_contingency": tmpl.get("is_contingency", False),
            "contingency_type": tmpl.get("contingency_type", None),
            "status": "pending",
        })

    return milestones


def days_to_close(closing_date: str) -> int:
    """Returns days remaining until closing. Negative = past due."""
    try:
        target = date.fromisoformat(closing_date)
        return (target - date.today()).days
    except ValueError:
        return 0


def milestone_status_summary(milestones: list[dict]) -> dict[str, Any]:
    """
    Summarize milestone progress.

    Returns:
        total, completed, pending, overdue, completion_pct
    """
    today = date.today().isoformat()
    total = len(milestones)
    completed = sum(1 for m in milestones if m.get("status") == "completed")
    overdue = sum(
        1 for m in milestones
        if m.get("status") != "completed" and m.get("target_date", "") < today
    )
    pending = total - completed

    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "overdue": overdue,
        "completion_pct": round(completed / total * 100, 1) if total > 0 else 0,
    }
