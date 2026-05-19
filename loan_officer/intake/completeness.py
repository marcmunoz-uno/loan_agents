"""
loan_officer/intake/completeness.py — Per-loan-type doc checklists and gap detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ── Loan product type ─────────────────────────────────────────────────────────

LoanProduct = Literal["dscr", "fix_flip", "bridge"]

# ── Checklist definitions ─────────────────────────────────────────────────────
# Each entry: {"doc_type": str, "label": str, "required": bool}

_CHECKLISTS: dict[str, list[dict]] = {
    "dscr": [
        {"doc_type": "rent_roll", "label": "Rent Roll / Lease Agreement", "required": True},
        {"doc_type": "bank_stmt", "label": "Bank Statements (3 months)", "required": True},
        {"doc_type": "purchase_contract", "label": "Purchase Contract", "required": True},
        {"doc_type": "appraisal", "label": "Appraisal / BPO", "required": False},
        {"doc_type": "insurance_binder", "label": "Insurance Binder", "required": False},
        {"doc_type": "entity_docs", "label": "Entity Docs (LLC/Corp)", "required": False},
    ],
    "fix_flip": [
        {"doc_type": "purchase_contract", "label": "Purchase Contract", "required": True},
        {"doc_type": "rehab_scope", "label": "Scope of Work / Rehab Budget", "required": True},
        {"doc_type": "bank_stmt", "label": "Bank Statements (3 months)", "required": True},
        {"doc_type": "appraisal", "label": "ARV Appraisal", "required": False},
        {"doc_type": "entity_docs", "label": "Entity Docs (LLC/Corp)", "required": False},
        {"doc_type": "id_government", "label": "Government-Issued ID", "required": True},
    ],
    "bridge": [
        {"doc_type": "purchase_contract", "label": "Purchase Contract", "required": True},
        {"doc_type": "bank_stmt", "label": "Bank Statements (3 months)", "required": True},
        {"doc_type": "appraisal", "label": "Appraisal / BPO", "required": True},
        {"doc_type": "payoff_statement", "label": "Payoff Statement (if refi)", "required": False},
        {"doc_type": "rent_roll", "label": "Rent Roll (if income property)", "required": False},
        {"doc_type": "entity_docs", "label": "Entity Docs (LLC/Corp)", "required": False},
        {"doc_type": "insurance_binder", "label": "Insurance Binder", "required": False},
    ],
}


# ── Gap detection ─────────────────────────────────────────────────────────────


@dataclass
class CompletenessReport:
    """Result of a completeness check for a given loan product."""

    product: str
    received: list[str]
    required_missing: list[str]
    optional_missing: list[str]
    is_complete: bool
    completion_pct: float              # 0–100, required docs only


def get_checklist(product: str) -> list[dict]:
    """Return the doc checklist for a loan product. Falls back to dscr if unknown."""
    return _CHECKLISTS.get(product, _CHECKLISTS["dscr"])


def check_completeness(product: str, received_doc_types: list[str]) -> CompletenessReport:
    """
    Compare received doc types against the product checklist.

    Returns a CompletenessReport with required_missing, optional_missing,
    is_complete (all required docs present), and completion_pct.

    TODO: iterate checklist, split into required vs optional gaps.
    """
    checklist = get_checklist(product)
    received_set = set(received_doc_types)

    required = [item for item in checklist if item["required"]]
    optional = [item for item in checklist if not item["required"]]

    required_missing = [
        item["doc_type"] for item in required if item["doc_type"] not in received_set
    ]
    optional_missing = [
        item["doc_type"] for item in optional if item["doc_type"] not in received_set
    ]

    total_required = len(required)
    received_required = total_required - len(required_missing)
    completion_pct = (received_required / total_required * 100) if total_required > 0 else 100.0

    return CompletenessReport(
        product=product,
        received=list(received_set),
        required_missing=required_missing,
        optional_missing=optional_missing,
        is_complete=len(required_missing) == 0,
        completion_pct=round(completion_pct, 1),
    )


def gap_message(report: CompletenessReport) -> str:
    """
    Return a human-readable summary of missing documents for borrower messaging.

    TODO: format message based on report.required_missing for chatbot display.
    """
    if report.is_complete:
        return "All required documents received."
    items = ", ".join(report.required_missing)
    return f"Still need: {items} to proceed with your {report.product.upper()} loan."
