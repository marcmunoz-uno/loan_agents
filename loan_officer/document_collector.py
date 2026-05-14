"""
loan_officer/document_collector.py — Document requirement tracking.

Tracks what documents a lender needs for a given loan application,
what's been received, and what's still outstanding.
"""

from __future__ import annotations
from typing import Literal

DocType = Literal[
    "government_id",
    "pay_stub",
    "bank_statement_3mo",
    "tax_return_2yr",
    "w2_2yr",
    "purchase_contract",
    "lease_agreement",
    "rental_market_analysis",
    "scope_of_work",
    "contractor_bids",
    "arv_appraisal",
    "entity_docs",
    "operating_agreement",
    "proof_of_insurance",
    "mortgage_statement",
    "photo_id",
    "authorization_form",
]

# Documents required per loan product
REQUIRED_DOCS: dict[str, list[str]] = {
    "dscr": [
        "government_id",
        "bank_statement_3mo",
        "purchase_contract",
        "lease_agreement",      # or rental market analysis if vacant
        "entity_docs",          # if borrowing in LLC
    ],
    "fix_flip": [
        "government_id",
        "bank_statement_3mo",
        "purchase_contract",
        "scope_of_work",
        "contractor_bids",
        "arv_appraisal",
        "entity_docs",
    ],
    "brrrr": [
        "government_id",
        "bank_statement_3mo",
        "purchase_contract",
        "scope_of_work",
        "contractor_bids",
        "lease_agreement",      # post-rehab, before refi
        "entity_docs",
    ],
    "conventional": [
        "government_id",
        "pay_stub",
        "bank_statement_3mo",
        "tax_return_2yr",
        "w2_2yr",
        "purchase_contract",
    ],
    "hard_money": [
        "government_id",
        "bank_statement_3mo",
        "purchase_contract",
        "scope_of_work",
    ],
    "private": [
        "government_id",
        "bank_statement_3mo",
        "purchase_contract",
        "entity_docs",
    ],
}

# Human-readable labels
DOC_LABELS: dict[str, str] = {
    "government_id": "Government-issued photo ID (driver's license or passport)",
    "pay_stub": "Most recent 2 pay stubs",
    "bank_statement_3mo": "Bank statements — last 3 months (all accounts)",
    "tax_return_2yr": "Federal tax returns — last 2 years (all pages)",
    "w2_2yr": "W-2 forms — last 2 years",
    "purchase_contract": "Signed purchase contract / PSA",
    "lease_agreement": "Current lease agreement (or signed lease for new tenant)",
    "rental_market_analysis": "Rental market analysis (if property is vacant)",
    "scope_of_work": "Detailed scope of work / rehab plan",
    "contractor_bids": "Contractor bids (at least 1, preferably 2)",
    "arv_appraisal": "After-repair value appraisal or BPO",
    "entity_docs": "LLC/entity formation docs + operating agreement",
    "operating_agreement": "Operating agreement (if multi-member LLC)",
    "proof_of_insurance": "Proof of property insurance",
    "mortgage_statement": "Most recent mortgage statement(s)",
    "photo_id": "Photo ID (same as government_id)",
    "authorization_form": "Signed authorization form",
}


def get_required_docs(product: str) -> list[dict[str, str]]:
    """Return the list of required documents for a product, with labels."""
    doc_types = REQUIRED_DOCS.get(product, REQUIRED_DOCS["dscr"])
    return [
        {"doc_type": dt, "label": DOC_LABELS.get(dt, dt), "required": True}
        for dt in doc_types
    ]


def missing_docs(
    product: str, docs_received: list[str]
) -> list[dict[str, str]]:
    """Return the documents still needed."""
    required = {d["doc_type"] for d in get_required_docs(product)}
    received = set(docs_received)
    missing = required - received
    return [
        {"doc_type": dt, "label": DOC_LABELS.get(dt, dt)}
        for dt in sorted(missing)
    ]


def docs_complete(product: str, docs_received: list[str]) -> bool:
    """Return True if all required documents have been received."""
    return len(missing_docs(product, docs_received)) == 0
