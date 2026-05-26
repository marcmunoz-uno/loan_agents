"""
loan_officer/typeform/letter_autofire.py — Typeform intake → prequal letter.

On every successful Typeform submission with at least one uploaded asset
statement URL, this module:

    1. Downloads the borrower's asset-statement PDFs from Typeform's CDN
    2. Runs each through Claude vision to extract `ending_balance`
    3. Sums the balances → `liquid_assets`
    4. Inserts a `loan_prequals` row using the intake's borrower data
       (property_data is empty — Typeform's qualification form is borrower-only)
    5. Calls `prequal_letter.generate_and_send(prequal_id, liquid_assets_override=...)`
       which renders the PDF, hosts it (S3 or self-hosted fallback), and
       emails the borrower the Munoz, Ghezlan & Co. letter via Zapier MCP
       Gmail Send — same skill we ship from `~/.claude/skills/tranchi-prequal-letter`.

If no statements were uploaded, or every OCR pass came back empty, the
letter is skipped — the existing soft-prequal email path takes over so the
borrower still gets a same-day response.

Designed to run inside a daemon thread spawned from the Flask webhook, so
Typeform's 10s webhook timeout is never blocked by OCR + PDF render + send.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from shared.db import get_conn, insert, update
from shared.llm import chat_with_vision

logger = logging.getLogger(__name__)

# Typeform CDN PDFs are usually a few hundred KB. Give us plenty of headroom.
_DOWNLOAD_TIMEOUT_S = 30
_MAX_DOC_BYTES = 20 * 1024 * 1024   # 20 MB
# Below this threshold the letter is useless — fall back to soft email.
_MIN_LIQUID_FOR_LETTER = 5_000.0

_ASSET_STATEMENT_FIELDS = (
    "asset_statement_recent_url",
    "asset_statement_previous_url",
    "asset_statement_extra_url",
)

_BALANCE_FIELDS = (
    "ending_balance",
    "available_balance",
    "current_balance",
    "balance",
)

_VISION_SYSTEM = (
    "You are a bank-statement balance extractor. Given a bank-statement "
    "PDF or image, return ONLY a single JSON object — no prose, no fences:\n"
    '  {"ending_balance": <number or null>, '
    '"bank_name": "<string or empty>", '
    '"statement_period": "<string or empty>"}\n\n'
    "The ending_balance is the closing/period-end balance, typically the "
    "final running balance shown after all transactions. Strip currency "
    "symbols and commas; return a plain number. If the document is not a "
    "bank statement, or no balance is identifiable, set ending_balance to null."
)


def fire_letter_async(intake_id: str, intake_row: dict[str, Any]) -> None:
    """
    Spawn a daemon thread that runs `_fire_letter_sync(intake_id, intake_row)`.
    The caller (Flask webhook) returns 200 immediately; the thread does the
    OCR + letter pipeline in the background.
    """
    t = threading.Thread(
        target=_fire_letter_sync,
        args=(intake_id, intake_row),
        name=f"prequal-letter-autofire-{intake_id}",
        daemon=True,
    )
    t.start()


def _fire_letter_sync(intake_id: str, intake_row: dict[str, Any]) -> dict[str, Any]:
    """
    Synchronous letter pipeline. Returns a status dict suitable for the audit
    row. Catches its own exceptions so a thread crash never silently swallows
    the work.
    """
    try:
        return _run(intake_id, intake_row)
    except Exception as e:
        logger.exception("[letter_autofire] %s pipeline failed", intake_id)
        _mark_intake(intake_id, status="letter_failed", error=str(e)[:500])
        return {"ok": False, "error": str(e)}


def _run(intake_id: str, intake_row: dict[str, Any]) -> dict[str, Any]:
    borrower_email = (intake_row.get("email") or "").strip()
    if not borrower_email:
        _mark_intake(intake_id, status="letter_skipped", error="no_borrower_email")
        return {"ok": False, "skipped": "no_borrower_email"}

    urls = [intake_row.get(f) or "" for f in _ASSET_STATEMENT_FIELDS]
    urls = [u for u in urls if u]
    if not urls:
        # Borrower didn't upload any asset statement — fall back to soft email.
        _mark_intake(intake_id, status="letter_skipped", error="no_asset_statements")
        return {"ok": False, "skipped": "no_asset_statements"}

    liquid_assets, per_doc = _ocr_asset_statements(urls)
    logger.info(
        "[letter_autofire] %s liquid_assets=%.2f from %d docs",
        intake_id, liquid_assets, len([d for d in per_doc if d.get("ending_balance")]),
    )

    if liquid_assets < _MIN_LIQUID_FOR_LETTER:
        _mark_intake(
            intake_id,
            status="letter_skipped",
            liquid_assets_computed=liquid_assets,
            error=f"liquid_assets_below_threshold ({liquid_assets:.0f} < {_MIN_LIQUID_FOR_LETTER:.0f})",
        )
        return {"ok": False, "skipped": "liquid_assets_below_threshold",
                "liquid_assets": liquid_assets, "per_doc": per_doc}

    prequal_id = _create_prequal_from_intake(intake_row, liquid_assets)

    # Defer the import — keeps webhook startup fast and lets the autofire
    # module be unit-tested without pulling reportlab into the import graph.
    from loan_officer.prequal_letter import generate_and_send

    result = generate_and_send(prequal_id, liquid_assets_override=liquid_assets)

    _mark_intake(
        intake_id,
        status="letter_sent" if result.zap_fired else "letter_failed",
        letter_id=result.letter_id,
        liquid_assets_computed=liquid_assets,
        email_subject="Your Pre-Qualification — Non-QM DSCR Loan",
        email_sent_at=result.issued_at if result.zap_fired else "",
        error="" if result.zap_fired else result.mcp_send_status,
    )

    return {
        "ok": result.zap_fired,
        "letter_id": result.letter_id,
        "prequal_id": prequal_id,
        "liquid_assets": liquid_assets,
        "max_pp_low": result.max_pp_low,
        "max_pp_high": result.max_pp_high,
        "mcp_send_status": result.mcp_send_status,
        "per_doc": per_doc,
    }


# ── OCR ──────────────────────────────────────────────────────────────────────

def _ocr_asset_statements(urls: list[str]) -> tuple[float, list[dict[str, Any]]]:
    """
    Download each URL, send to Claude vision, parse `ending_balance`. Sum
    the parsed balances across all docs. Returns (sum, per_doc_breakdown).
    """
    total = 0.0
    breakdown: list[dict[str, Any]] = []
    for url in urls:
        entry: dict[str, Any] = {"url": url}
        try:
            data, media_type = _download(url)
            entry["bytes"] = len(data)
            entry["media_type"] = media_type
            raw = chat_with_vision(
                prompt="Extract the bank-statement ending_balance as JSON.",
                media=[{"data": data, "media_type": media_type}],
                system=_VISION_SYSTEM,
                model_tier="standard",
                max_tokens=400,
            )
            parsed = _parse_balance_response(raw)
            entry.update(parsed)
            bal = parsed.get("ending_balance")
            if isinstance(bal, (int, float)) and bal > 0:
                total += float(bal)
        except Exception as e:
            entry["error"] = str(e)[:200]
            logger.warning("[letter_autofire] OCR failed for %s: %s", url, e)
        breakdown.append(entry)
    return round(total, 2), breakdown


def _download(url: str) -> tuple[bytes, str]:
    """
    Fetch the file. Returns (bytes, media_type). Raises on network / size errors.

    Typeform file_url answers point at `api.typeform.com/responses/files/...`
    which requires a Personal Access Token. We inject the Bearer header when
    the URL host is Typeform's API; other hosts (public CDN, S3, etc.) get
    no auth.
    """
    headers: dict[str, str] = {}
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("typeform.com"):
        token = os.environ.get("TYPEFORM_ACCESS_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(url, headers=headers, timeout=_DOWNLOAD_TIMEOUT_S, stream=True)
    resp.raise_for_status()
    media_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if not media_type:
        # Best-effort guess from URL extension.
        ext = (url.rsplit(".", 1)[-1] or "").lower()
        media_type = {
            "pdf": "application/pdf",
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp", "gif": "image/gif",
        }.get(ext, "application/octet-stream")
    data = resp.content
    if len(data) > _MAX_DOC_BYTES:
        raise ValueError(f"download exceeds {_MAX_DOC_BYTES} bytes ({len(data)})")
    return data, media_type


def _parse_balance_response(raw: str) -> dict[str, Any]:
    """
    Pull `{"ending_balance": ...}` out of the model's reply. Tolerant of
    ```fenced``` JSON and leading prose.
    """
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {"ending_balance": None, "raw": text[:200]}
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"ending_balance": None, "raw": text[:200]}
    if not isinstance(obj, dict):
        return {"ending_balance": None, "raw": text[:200]}
    bal = _coerce_money(obj.get("ending_balance"))
    if bal is None:
        for k in _BALANCE_FIELDS:
            if k in obj:
                bal = _coerce_money(obj[k])
                if bal is not None:
                    break
    return {
        "ending_balance": bal,
        "bank_name": str(obj.get("bank_name") or ""),
        "statement_period": str(obj.get("statement_period") or ""),
    }


def _coerce_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


# ── DB writes ────────────────────────────────────────────────────────────────

def _create_prequal_from_intake(intake_row: dict[str, Any], liquid_assets: float) -> str:
    """
    Insert a minimal `loan_prequals` row using the Typeform intake's borrower
    data. Property data is empty — the Typeform qualification form is
    borrower-only; the letter quotes a max-PP range, not a specific property.
    """
    prequal_id = f"pq_{uuid.uuid4().hex[:12]}"
    first = intake_row.get("first_name") or ""
    last = intake_row.get("last_name") or ""
    borrower_data = {
        "name": f"{first} {last}".strip() or "Borrower",
        "email": intake_row.get("email") or "",
        "phone": intake_row.get("phone") or "",
        "credit_score": intake_row.get("credit_score_estimate"),
        "liquidity": liquid_assets,
        "company": intake_row.get("company") or "",
        "source": "typeform_autofire",
    }
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": prequal_id,
        "user_id": intake_row.get("email") or intake_row.get("intake_id") or "",
        "borrower_data": json.dumps(borrower_data),
        "property_data": json.dumps({}),
        "score": float(intake_row.get("soft_prequal_score") or 0),
        "suggested_product": "dscr",
        "dscr": None,
        "ltv": None,
        "monthly_payment_estimate": 0.0,
        "strengths": json.dumps([]),
        "concerns": json.dumps([]),
        "next_steps": json.dumps([]),
        "status": "from_typeform",
        "notes": f"Auto-created from typeform intake {intake_row.get('intake_id', '')}",
        "created_at": now,
        "updated_at": now,
    }
    with get_conn() as conn:
        insert(conn, "loan_prequals", row)
    return prequal_id


def _mark_intake(
    intake_id: str,
    *,
    status: str,
    letter_id: str = "",
    liquid_assets_computed: Optional[float] = None,
    email_subject: str = "",
    email_sent_at: str = "",
    error: str = "",
) -> None:
    """
    Best-effort intake-row update. The schema may or may not have the newer
    columns (`letter_id`, `liquid_assets_computed`) depending on whether
    migration 006 has run — we update what exists and ignore the rest.
    """
    fields: dict[str, Any] = {"email_send_status": status}
    if letter_id:
        fields["letter_id"] = letter_id
    if liquid_assets_computed is not None:
        fields["liquid_assets_computed"] = liquid_assets_computed
    if email_subject:
        fields["email_subject"] = email_subject
    if email_sent_at:
        fields["email_sent_at"] = email_sent_at
    if error:
        fields["email_error"] = error[:500]
    try:
        with get_conn() as conn:
            update(conn, "loan_borrower_intakes", fields, "intake_id = ?", (intake_id,))
    except Exception as e:
        # Newer column missing on older schemas — retry without optional fields.
        if "letter_id" in str(e) or "liquid_assets_computed" in str(e):
            stripped = {k: v for k, v in fields.items()
                        if k not in ("letter_id", "liquid_assets_computed")}
            try:
                with get_conn() as conn:
                    update(conn, "loan_borrower_intakes", stripped,
                           "intake_id = ?", (intake_id,))
            except Exception:
                logger.exception("[letter_autofire] intake update failed for %s", intake_id)
        else:
            logger.exception("[letter_autofire] intake update failed for %s", intake_id)
