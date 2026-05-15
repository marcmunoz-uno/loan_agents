"""
loan_officer/arive_zapier.py — Arive LOS integration via Zapier webhooks.

Two-way integration:
  Outbound: our events → Zapier → Arive (creates/updates records)
  Inbound:  Arive events → Zapier → POST /api/loan/webhook/arive-update → us

All outbound webhook URLs come from env vars so the module works safely in dev
with empty defaults (fire_zap is a no-op when the URL is not configured).

Idempotency: every payload carries a `correlation_id` field so Zapier/Arive
re-processing a retry does not double-create records.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Zapier webhook URL registry
# ---------------------------------------------------------------------------

ZAPIER_WEBHOOKS: dict[str, str] = {
    "prequal_created":          os.environ.get("ZAPIER_HOOK_PREQUAL_CREATED", ""),
    "application_submitted":    os.environ.get("ZAPIER_HOOK_APPLICATION_SUBMITTED", ""),
    "documents_uploaded":       os.environ.get("ZAPIER_HOOK_DOCUMENTS_UPLOADED", ""),
    "ready_for_underwriting":   os.environ.get("ZAPIER_HOOK_READY_FOR_UNDERWRITING", ""),
    "lender_routed":            os.environ.get("ZAPIER_HOOK_LENDER_ROUTED", ""),
    "approved":                 os.environ.get("ZAPIER_HOOK_APPROVED", ""),
    "declined":                 os.environ.get("ZAPIER_HOOK_DECLINED", ""),
    "funded":                   os.environ.get("ZAPIER_HOOK_FUNDED", ""),
}

# Arive status vocabulary → our internal state
ARIVE_STATUS_MAP: dict[str, str] = {
    # Disclosure / submission stages
    "Initial Disclosures Sent":         "APP_SUBMITTED",
    "Submitted to Underwriting":        "UNDERWRITING",
    "Conditional Approval":             "CONDITIONS",
    "Suspended":                        "CONDITIONS",
    "Cleared to Close":                 "CLOSING",
    "Closing Disclosure Sent":          "CLOSING",
    "Docs Out":                         "CLOSING",
    "Funded":                           "FUNDED",
    "Approved":                         "APPROVED",
    "Declined":                         "DECLINED",
    "Withdrawn":                        "DECLINED",
    # Pre-submission
    "Application Started":              "APP_STARTED",
    "Application Submitted":            "APP_SUBMITTED",
    "Processing":                       "APP_SUBMITTED",
    "Ready for Submission":             "APP_SUBMITTED",
}

_REQUEST_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Outbound: fire_zap
# ---------------------------------------------------------------------------

def fire_zap(
    event_type: str,
    payload: dict[str, Any],
    correlation_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    POST payload to the Zapier webhook for `event_type`.

    Returns a result dict:
        {"success": True, "event_type": "...", "correlation_id": "..."}
        {"success": False, "event_type": "...", "error": "...", "skipped": True}

    Skips (no-op) when the webhook URL is not configured — safe in dev.
    Injects a `correlation_id` for idempotency; callers can pass their own
    (e.g. the application ID) so retries are deduplicated in Arive.
    """
    url = ZAPIER_WEBHOOKS.get(event_type, "")
    if not url:
        logger.debug("[arive_zapier] %s: no webhook URL configured — skipping", event_type)
        return {"success": False, "event_type": event_type, "skipped": True, "error": "No webhook URL configured"}

    if correlation_id is None:
        # Deterministic from event_type + primary entity ID so retries are safe
        raw = f"{event_type}:{payload.get('application_id') or payload.get('prequal_id') or uuid.uuid4().hex}"
        correlation_id = hashlib.sha256(raw.encode()).hexdigest()[:24]

    outbound = {
        **payload,
        "correlation_id": correlation_id,
        "event_type": event_type,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "source": "tranchi.ai",
    }

    try:
        resp = requests.post(
            url,
            json=outbound,
            timeout=_REQUEST_TIMEOUT,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        logger.info(
            "[arive_zapier] %s fired → %s (%d) corr=%s",
            event_type, url, resp.status_code, correlation_id,
        )
        return {
            "success": True,
            "event_type": event_type,
            "correlation_id": correlation_id,
            "status_code": resp.status_code,
        }
    except requests.exceptions.RequestException as exc:
        logger.error("[arive_zapier] %s POST failed: %s", event_type, exc)
        return {
            "success": False,
            "event_type": event_type,
            "correlation_id": correlation_id,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Field mapping: internal → Arive-friendly flat JSON
# ---------------------------------------------------------------------------

def to_arive_format(
    record: dict[str, Any],
    correlation_id: Optional[str] = None,
    mlo_assignment: str = "round_robin",
) -> dict[str, Any]:
    """
    Convert a prequal or application record (with nested borrower/property dicts)
    into the flat Arive-friendly schema Zapier expects.

    borrower_data and property_data can be either JSON strings or dicts.
    """
    def _load(v: Any) -> dict:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return {}
        return v or {}

    b = _load(record.get("borrower_data", {}))
    p = _load(record.get("property_data", {}))

    # Parse address string: "4521 Oak Ln, Detroit MI 48224"
    # Best-effort: take last segment as zip, second-to-last as state+city
    address_str = p.get("address", "")
    addr_parts = [s.strip() for s in address_str.split(",")]
    street = addr_parts[0] if addr_parts else address_str
    city_state_zip = addr_parts[1] if len(addr_parts) > 1 else ""
    csz_tokens = city_state_zip.split()
    zip_code = csz_tokens[-1] if csz_tokens and csz_tokens[-1].isdigit() else ""
    state = csz_tokens[-2] if len(csz_tokens) >= 2 else ""
    city = " ".join(csz_tokens[:-2]) if len(csz_tokens) >= 3 else (csz_tokens[0] if csz_tokens else "")

    # Compute DSCR
    dscr = record.get("dscr")
    monthly_rent = p.get("monthly_rent", 0)
    monthly_piti = record.get("monthly_payment_estimate", 0)

    # Loan purpose normalization
    raw_purpose = b.get("loan_purpose", "purchase")
    purpose_map = {
        "purchase": "purchase",
        "refinance": "refi",
        "cash_out_refi": "cash_out",
        "construction": "purchase",
    }
    loan_purpose = purpose_map.get(raw_purpose, raw_purpose)

    # Product normalization
    product_raw = record.get("suggested_product", "dscr")
    product_display_map = {
        "dscr":         "DSCR",
        "fix_flip":     "Fix & Flip",
        "brrrr":        "BRRRR / DSCR",
        "conventional": "Conventional",
        "hard_money":   "Hard Money",
        "private":      "Private",
        "multifamily":  "Multifamily",
    }
    loan_product = product_display_map.get(product_raw, product_raw.upper())

    # Down payment
    purchase_price = p.get("purchase_price", 0)
    down_payment = b.get("down_payment", 0)
    down_pct = b.get("down_payment_pct", 0)
    loan_amount = b.get("desired_loan_amount", 0)
    if loan_amount and purchase_price and not down_payment:
        down_payment = purchase_price - loan_amount
    if down_payment and purchase_price and not down_pct:
        down_pct = round((down_payment / purchase_price) * 100, 2) if purchase_price else 0

    # Borrower name split
    full_name = b.get("name", "")
    name_parts = full_name.strip().split(None, 1)
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    return {
        # Loan details
        "loan_amount":              loan_amount or (purchase_price - down_payment),
        "purchase_price":           purchase_price,
        "down_payment":             down_payment,
        "down_payment_pct":         down_pct,
        "loan_purpose":             loan_purpose,
        "loan_product":             loan_product,
        # Property
        "property_address":         street,
        "property_city":            city,
        "property_state":           state,
        "property_zip":             zip_code,
        "subject_property_type":    p.get("property_type", "single_family"),
        # Borrower
        "borrower_first_name":      first_name,
        "borrower_last_name":       last_name,
        "borrower_email":           b.get("email", ""),
        "borrower_phone":           b.get("phone", ""),
        "borrower_fico_estimate":   b.get("credit_score") or 0,
        # DSCR / rental
        "estimated_monthly_rent":   monthly_rent,
        "estimated_dscr":           dscr,
        # Assignment / intake
        "mlo_assignment":           mlo_assignment,
        "intake_source":            "tranchi.ai",
        "correlation_id":           correlation_id or record.get("id", ""),
    }
