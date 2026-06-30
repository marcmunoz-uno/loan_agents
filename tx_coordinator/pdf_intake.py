"""
tx_coordinator/pdf_intake.py — PSA PDF → PSATerms via Claude vision.

Real PSAs arrive as PDFs, not JSON. This module accepts a raw PDF byte string,
hands it to Claude vision, and returns a structured PSATerms candidate plus a
DB row id (`tx_psa_intakes.id`) that the caller can reference when confirming
or correcting the extraction.

Two-phase flow on purpose:

    1. POST /api/tx/open-from-pdf  → returns extracted terms + intake_id (no tx yet)
    2. POST /api/tx/open           → caller reviews/corrects, then opens the tx
                                      passing intake_id so we can mark it accepted

Extraction is intentionally permissive — we ask Claude for best-effort JSON and
expose whatever came back. The caller is the source of truth on what gets
written into `transactions`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db import get_conn, insert, update
from shared.llm import chat_with_vision

_EXTRACTION_PROMPT = """\
You are extracting terms from a real-estate Purchase and Sale Agreement (PSA).
Return ONE JSON object with the following fields (use empty string or 0 if
truly unstated, never invent — explicit "" or 0 is fine):

{
  "purchase_price":               number,
  "earnest_money":                number,
  "closing_date":                 "YYYY-MM-DD",
  "psa_execution_date":           "YYYY-MM-DD",
  "inspection_period_days":       integer,
  "financing_contingency_days":   integer,
  "title_contingency_days":       integer,
  "seller_concessions":           number,
  "buyer_name":                   string,
  "buyer_email":                  string,
  "buyer_phone":                  string,
  "seller_name":                  string,
  "seller_email":                 string,
  "seller_phone":                 string,
  "buyer_agent_name":             string,
  "listing_agent_name":           string,
  "property_address":             string,
  "notes":                        string
}

Rules:
- Dates must be YYYY-MM-DD. If only month/day appears with a year on the
  signature page, use that.
- inspection_period_days, financing_contingency_days, title_contingency_days
  must reflect what the PSA actually says — do NOT default. If the doc is
  silent, set the field to 0.
- Reply with ONLY the JSON object. No prose, no markdown fences, no commentary.
"""


def extract_psa_terms(pdf_bytes: bytes, *, source_url: str = "") -> dict[str, Any]:
    """
    Run the PSA through Claude vision and store the extraction. Returns a dict
    with keys: intake_id, extracted_terms (dict), extraction_status, error.
    """
    now = datetime.now(timezone.utc).isoformat()

    intake_id = _new_intake_row(source_url=source_url, uploaded_at=now)

    try:
        raw = chat_with_vision(
            prompt=_EXTRACTION_PROMPT,
            media=[{"data": pdf_bytes, "media_type": "application/pdf"}],
            max_tokens=2048,
            temperature=0.0,
        )
    except Exception as e:  # noqa: BLE001 — propagate as a failed extraction
        _mark_failure(intake_id, error=repr(e))
        return {
            "intake_id": intake_id,
            "extraction_status": "failed",
            "error": repr(e),
            "extracted_terms": {},
        }

    parsed = _safe_json_parse(raw)
    if parsed is None:
        _mark_failure(intake_id, error=f"Could not JSON-parse model output: {raw[:300]!r}")
        return {
            "intake_id": intake_id,
            "extraction_status": "failed",
            "error": "non_json_response",
            "extracted_terms": {"raw": raw},
        }

    _mark_success(intake_id, terms=parsed)
    return {
        "intake_id": intake_id,
        "extraction_status": "extracted",
        "error": "",
        "extracted_terms": parsed,
    }


def accept_intake(intake_id: int, tx_id: str) -> bool:
    """Attach an intake to a freshly-opened transaction."""
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE tx_psa_intakes
               SET transaction_id = ?, extraction_status = 'accepted', accepted_at = ?
               WHERE id = ?""",
            (tx_id, now, intake_id),
        )
        conn.commit()
        return cur.rowcount > 0


# ── internals ─────────────────────────────────────────────────────────────────


def _new_intake_row(*, source_url: str, uploaded_at: str) -> int:
    with get_conn() as conn:
        return insert(conn, "tx_psa_intakes", {
            "source_url": source_url,
            "extracted_terms": "{}",
            "extraction_status": "pending",
            "extraction_error": "",
            "extraction_model": "claude-sonnet-4-5-20250929",
            "uploaded_at": uploaded_at,
        })


def _mark_failure(intake_id: int, *, error: str) -> None:
    with get_conn() as conn:
        update(conn, "tx_psa_intakes",
               {"extraction_status": "failed", "extraction_error": error[:2000]},
               where="id = ?", where_params=(intake_id,))


def _mark_success(intake_id: int, *, terms: dict) -> None:
    with get_conn() as conn:
        update(conn, "tx_psa_intakes",
               {"extraction_status": "extracted",
                "extracted_terms": json.dumps(terms),
                "extraction_error": ""},
               where="id = ?", where_params=(intake_id,))


def _safe_json_parse(raw: str) -> Optional[dict]:
    """Strip stray fences / prose and try to parse as JSON."""
    text = raw.strip()
    if text.startswith("```"):
        # ```json ... ``` or ``` ... ```
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
        text = text.rstrip("`").strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return None
