"""
tx_coordinator/document_vault.py — Transaction document tracking.

Tracks PSA, addendums, disclosures, inspection reports, title commitment,
appraisal, loan approval letter, closing disclosure, HUD-1/ALTA, deed.
"""

from __future__ import annotations
from typing import Any, Optional

from shared.db import get_conn, fetchall, fetchone


CORE_DOCUMENTS = [
    ("psa", "Purchase and Sale Agreement (signed)"),
    ("earnest_money_receipt", "Earnest Money Receipt"),
    ("title_commitment", "Title Commitment / Preliminary Title Report"),
    ("inspection_report", "Inspection Report"),
    ("appraisal", "Appraisal Report"),
    ("loan_approval", "Loan Approval Letter"),
    ("closing_disclosure", "Closing Disclosure (CD)"),
    ("proof_of_insurance", "Property Insurance Binder"),
    ("final_walkthrough_form", "Final Walk-Through Form"),
    ("deed", "Recorded Deed"),
]

OPTIONAL_DOCUMENTS = [
    ("addendum", "Addendum(s) to PSA"),
    ("inspection_response", "Inspection Response / Repair Request"),
    ("seller_disclosure", "Seller's Disclosure Statement"),
    ("hoa_docs", "HOA Docs (if applicable)"),
    ("survey", "Property Survey"),
    ("wire_instructions", "Wiring Instructions"),
    ("hud1_alta", "HUD-1 or ALTA Settlement Statement"),
]


def get_documents(tx_id: str) -> list[dict]:
    with get_conn() as conn:
        return fetchall(conn, "SELECT * FROM tx_documents WHERE transaction_id = ?", (tx_id,))


def get_document_by_type(tx_id: str, doc_type: str) -> Optional[dict]:
    with get_conn() as conn:
        rows = fetchall(
            conn,
            "SELECT * FROM tx_documents WHERE transaction_id = ? AND doc_type = ? ORDER BY uploaded_at DESC LIMIT 1",
            (tx_id, doc_type)
        )
    return rows[0] if rows else None


def missing_core_documents(tx_id: str) -> list[dict[str, str]]:
    """Return core documents not yet in the vault."""
    docs = get_documents(tx_id)
    received = {d["doc_type"] for d in docs if d["status"] not in ("rejected", "superseded")}
    return [
        {"doc_type": dt, "label": label}
        for dt, label in CORE_DOCUMENTS
        if dt not in received
    ]


def document_checklist(tx_id: str) -> dict[str, Any]:
    """Return full document checklist with status per document."""
    docs = get_documents(tx_id)
    received_map = {d["doc_type"]: d for d in docs}

    core = []
    for dt, label in CORE_DOCUMENTS:
        doc = received_map.get(dt)
        core.append({
            "doc_type": dt,
            "label": label,
            "required": True,
            "received": doc is not None,
            "status": doc["status"] if doc else "missing",
            "s3_url": doc["s3_url"] if doc else None,
        })

    optional = []
    for dt, label in OPTIONAL_DOCUMENTS:
        doc = received_map.get(dt)
        optional.append({
            "doc_type": dt,
            "label": label,
            "required": False,
            "received": doc is not None,
            "status": doc["status"] if doc else "missing",
            "s3_url": doc["s3_url"] if doc else None,
        })

    total_required = len(CORE_DOCUMENTS)
    received_required = sum(1 for item in core if item["received"])

    return {
        "completion_pct": round(received_required / total_required * 100, 1),
        "core": core,
        "optional": optional,
    }
