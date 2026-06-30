"""
loan_officer/intake/statement_liquidity.py — On-demand bank-statement liquidity.

Given a borrower email (or explicit file URLs), pull their uploaded asset
statements from Typeform, OCR each with Claude vision, and return a defensible
liquidity figure. This runs server-side where TYPEFORM_ACCESS_TOKEN and
ANTHROPIC_API_KEY already live, so no secret has to leave the deployment.

Liquidity is aggregated by ACCOUNT, not summed across statements: the form
collects a "recent" and a "previous" month, which are usually the same account
— summing them would double-count. We take the largest balance per
(bank, account-last4) and drop implausible OCR reads.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from shared.llm import chat_with_vision
from shared.net import assert_safe_url
from loan_officer.typeform.mapper import map_payload
from loan_officer.prequal_letter import MAX_PLAUSIBLE_STATEMENT_BALANCE, _account_key

logger = logging.getLogger(__name__)

# The live Pre-Qualification ("qualification") form. Overridable via env in case
# the form is re-published under a new id.
DEFAULT_FORM_ID = os.environ.get("TYPEFORM_QUALIFICATION_FORM_ID", "nZepgsCC")

_DOWNLOAD_TIMEOUT_S = 30
_MAX_DOC_BYTES = 20 * 1024 * 1024

_ASSET_STATEMENT_KEYS = (
    "asset_statement_recent_url",
    "asset_statement_previous_url",
    "asset_statement_extra_url",
)

_VISION_SYSTEM = (
    "You are a bank-statement extractor. Given a bank-statement PDF or image, "
    "return ONLY a single JSON object — no prose, no fences:\n"
    '  {"ending_balance": <number or null>, "bank_name": "<string or empty>", '
    '"account_last4": "<last 4 digits of the account number or empty>", '
    '"statement_period": "<string or empty>"}\n\n'
    "ending_balance is the closing/period-end balance (the final running "
    "balance after all transactions). Strip currency symbols and commas; return "
    "a plain number. If the document is not a bank statement or no balance is "
    "identifiable, set ending_balance to null."
)


class TypeformTokenMissing(RuntimeError):
    """Raised when TYPEFORM_ACCESS_TOKEN is needed but not configured."""


class BorrowerResponseNotFound(RuntimeError):
    """Raised when no Typeform response matches the borrower."""


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


def fetch_asset_statement_urls(email: str, form_id: Optional[str] = None) -> dict[str, Any]:
    """
    Query the Typeform Responses API for the borrower's most recent submission
    and return the asset-statement file URLs (recent / previous / extra).

    Requires TYPEFORM_ACCESS_TOKEN. Raises TypeformTokenMissing or
    BorrowerResponseNotFound.
    """
    token = os.environ.get("TYPEFORM_ACCESS_TOKEN", "").strip()
    if not token:
        raise TypeformTokenMissing("TYPEFORM_ACCESS_TOKEN is not set")
    form_id = form_id or DEFAULT_FORM_ID

    resp = requests.get(
        f"https://api.typeform.com/forms/{form_id}/responses",
        headers={"Authorization": f"Bearer {token}"},
        params={"query": email, "page_size": 1},
        timeout=_DOWNLOAD_TIMEOUT_S,
    )
    resp.raise_for_status()
    items = (resp.json() or {}).get("items") or []
    if not items:
        raise BorrowerResponseNotFound(f"no Typeform response for {email!r} on form {form_id}")

    mapped = map_payload(items[0])
    urls = {k: mapped.get(k, "") for k in _ASSET_STATEMENT_KEYS}
    return {
        "form_id": form_id,
        "typeform_response_id": mapped.get("typeform_response_id", ""),
        "submitted_at": mapped.get("submitted_at", ""),
        "first_name": mapped.get("first_name", ""),
        "last_name": mapped.get("last_name", ""),
        "email": mapped.get("email", ""),
        "credit_score_estimate": mapped.get("credit_score_estimate"),
        "asset_statement_urls": [u for u in urls.values() if u],
    }


def _download(url: str) -> tuple[bytes, str]:
    """Fetch a statement file. Injects the Typeform PAT for api.typeform.com."""
    assert_safe_url(url)
    headers: dict[str, str] = {}
    host = (urlparse(url).hostname or "").lower()
    if host == "api.typeform.com":
        token = os.environ.get("TYPEFORM_ACCESS_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(url, headers=headers, timeout=_DOWNLOAD_TIMEOUT_S, stream=True)
    resp.raise_for_status()
    media_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    if not media_type:
        ext = (url.rsplit(".", 1)[-1] or "").lower()
        media_type = {
            "pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp", "gif": "image/gif",
        }.get(ext, "application/octet-stream")
    data = resp.content
    if len(data) > _MAX_DOC_BYTES:
        raise ValueError(f"download exceeds {_MAX_DOC_BYTES} bytes ({len(data)})")
    return data, media_type


def _parse_vision(raw: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip(), flags=re.IGNORECASE).strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"ending_balance": None}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"ending_balance": None}
    if not isinstance(obj, dict):
        return {"ending_balance": None}
    return {
        "ending_balance": _coerce_money(obj.get("ending_balance")),
        "bank_name": str(obj.get("bank_name") or ""),
        "account_last4": str(obj.get("account_last4") or ""),
        "statement_period": str(obj.get("statement_period") or ""),
    }


def ocr_statements(urls: list[str]) -> list[dict[str, Any]]:
    """Download + OCR each statement URL. Returns a per-document breakdown."""
    out: list[dict[str, Any]] = []
    for url in urls:
        entry: dict[str, Any] = {"url": url}
        try:
            data, media_type = _download(url)
            raw = chat_with_vision(
                prompt="Extract the bank-statement fields as JSON.",
                media=[{"data": data, "media_type": media_type}],
                system=_VISION_SYSTEM,
                model_tier="standard",
                max_tokens=400,
            )
            entry.update(_parse_vision(raw))
        except Exception as e:  # noqa: BLE001 — one bad statement shouldn't kill the rest
            entry["error"] = str(e)[:200]
            entry["ending_balance"] = None
            logger.warning("[statement_liquidity] OCR failed for %s: %s", url, e)
        out.append(entry)
    return out


def aggregate_liquidity(per_doc: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Dedupe statements by account (largest balance per bank+last4) and drop
    implausible reads. Returns the liquidity total + accounting detail.
    """
    by_account: dict[str, float] = {}
    anon = 0
    for d in per_doc:
        bal = d.get("ending_balance")
        if not isinstance(bal, (int, float)) or bal <= 0:
            d.setdefault("counted", False)
            continue
        if bal > MAX_PLAUSIBLE_STATEMENT_BALANCE:
            d["counted"] = False
            d["skipped_reason"] = f"implausible balance {bal:.0f} (likely OCR error)"
            continue
        key = _account_key(d) or f"_anon_{anon}"
        if key.startswith("_anon_"):
            anon += 1
        if bal > by_account.get(key, 0.0):
            by_account[key] = bal
        d["counted"] = True
        d["account_key"] = key
    return {
        "liquid_assets": round(sum(by_account.values()), 2),
        "num_accounts": len(by_account),
        "num_statements_ocrd": len(per_doc),
        "breakdown": per_doc,
    }


def compute_liquidity_for_email(email: str, form_id: Optional[str] = None) -> dict[str, Any]:
    """End-to-end: resolve statement URLs for a borrower → OCR → liquidity."""
    found = fetch_asset_statement_urls(email, form_id)
    per_doc = ocr_statements(found["asset_statement_urls"])
    agg = aggregate_liquidity(per_doc)
    return {**found, **agg}


def compute_liquidity_for_urls(urls: list[str]) -> dict[str, Any]:
    """Liquidity for explicit statement URLs (skips the Typeform lookup)."""
    per_doc = ocr_statements(urls)
    return aggregate_liquidity(per_doc)
