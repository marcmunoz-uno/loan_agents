"""
loan_officer/arive_create_loan.py — Create a loan record in Arive via Zapier MCP.

The Arive `create_loan` action is the LO's natural next step after the borrower
goes under contract:
  1. We push borrower + property + PSA terms to Arive
  2. Arive creates the loan file
  3. Arive emails the borrower an invitation to fill out the 1003 in the POS

Wired to fire automatically from the PSA-classification path: as soon as the
borrower's reply lands and Claude vision tags it as a purchase_contract, this
fires alongside the deal-flow TX open. Both are independent — either failing
doesn't block the other.

Configuration:
    LO_SIGNER_EMAIL    — the LO's email, used as Arive `originatorEmail`
    ZAPIER_MCP_ENDPOINT + ZAPIER_MCP_API_KEY — same as the prequal-letter Gmail send
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from shared.zapier_mcp import ZapierMCPClient

# ── Constants ────────────────────────────────────────────────────────────────

ARIVE_APP = "arive"
ARIVE_ACTION_CREATE_LOAN = "create_loan"

DEFAULT_ORIGINATOR_EMAIL = os.environ.get("LO_SIGNER_EMAIL", "marc@munoz.ltd")

# Our property_type strings → Arive's propertyType enum.
PROPERTY_TYPE_MAP = {
    "single_family":          "SINGLE_FAMILY_DETACHED",
    "single_family_detached": "SINGLE_FAMILY_DETACHED",
    "duplex":                 "TWO_UNIT",
    "multi_family_2_4":       "TWO_UNIT",
    "triplex":                "THREE_UNIT",
    "fourplex":               "FOUR_UNIT",
    "condo":                  "CONDO_UNDER_5_STORIES",
    "townhouse":              "TOWNHOUSE",
    "townhome":               "TOWNHOUSE",
    "multifamily_5plus":      "FIVE_UNIT",
    "commercial":             "MIXED_USE_PROPERTY",
    "land":                   "VACANT_LOT_LAND",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def split_name(full_name: str) -> tuple[str, str]:
    """
    Split a borrower's display name into (first, last). Handles LLC titles by
    taking the part before the first comma:
        "Michael Sonagiers, President, Sonagiers, LLC" → ("Michael", "Sonagiers")
        "Marc Munoz"                                   → ("Marc", "Munoz")
        "Cher"                                          → ("Cher", "")
    """
    if not full_name:
        return ("", "")
    head = full_name.split(",", 1)[0].strip()
    parts = head.split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (" ".join(parts[:-1]), parts[-1])


_STATE_ZIP_RE = re.compile(r"^([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")


def parse_address(address: str) -> dict[str, str]:
    """
    Best-effort US-address parse. Common Tranchi PSA format:
        "<street>, <city>, [<county>,] <STATE> <ZIP>"

    Returns the four Arive subjectProperty_* components (empty strings if
    a component can't be parsed). Never raises.
    """
    out = {"addressLineText": "", "city": "", "county": "", "state": "", "postalCode": ""}
    if not address:
        return out
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return out
    out["addressLineText"] = parts[0]
    last = parts[-1]
    m = _STATE_ZIP_RE.match(last)
    middle: list[str]
    if m:
        out["state"] = m.group(1)
        out["postalCode"] = m.group(2)
        middle = parts[1:-1]
    else:
        middle = parts[1:]
    if middle:
        out["city"] = middle[0]
    if len(middle) > 1 and "county" in middle[1].lower():
        out["county"] = middle[1]
    return out


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


def _coerce_phone10(value: Any) -> str:
    """Strip non-digits, return the last 10 (Arive's 10-digit format)."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    return digits[-10:] if len(digits) >= 10 else ""


# ── Field mapping ────────────────────────────────────────────────────────────

def map_to_arive_params(
    prequal: dict[str, Any],
    extracted_psa_fields: dict[str, Any],
    *,
    originator_email: Optional[str] = None,
) -> dict[str, Any]:
    """
    Build the Arive `create_loan` params dict from our prequal + PSA data.
    Only includes fields with non-empty values so Arive's optional defaults
    apply where we don't have data.
    """
    borrower = prequal.get("borrower_data") or {}
    prop = prequal.get("property_data") or {}

    first, last = split_name(str(borrower.get("name", "")))
    addr = parse_address(
        str(extracted_psa_fields.get("property_address", "") or prop.get("address", ""))
    )

    purchase_price = (
        _coerce_money(extracted_psa_fields.get("purchase_price"))
        or _coerce_money(prop.get("purchase_price"))
        or 0.0
    )
    desired = _coerce_money(borrower.get("desired_loan_amount")) or 0.0
    base_loan = desired if desired > 0 else purchase_price * 0.75

    params: dict[str, Any] = {
        # ── Required ────────────────────────────────────────────────────────
        "loanPurpose":               "Purchase",
        "originatorEmail":           originator_email or DEFAULT_ORIGINATOR_EMAIL,
        "borrower1_firstName":       first or "Borrower",
        "borrower1_lastName":        last or first or "Unknown",
        "borrower1_emailAddressText":str(borrower.get("email") or ""),
        # ── DSCR-investor defaults ──────────────────────────────────────────
        "mortgageType":              "NonQM",
        "propertyUsageType":         "Investment",
        "homebuyingStage":           "UNDER_CONTRACT",
        # ── Provenance ──────────────────────────────────────────────────────
        "externalCreateDate":        datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "crmReferenceId":            str(prequal.get("id", "")),
    }

    pt = str(prop.get("property_type") or "").lower()
    if pt in PROPERTY_TYPE_MAP:
        params["propertyType"] = PROPERTY_TYPE_MAP[pt]

    fico = borrower.get("credit_score")
    if fico:
        try:
            params["estimatedFICO"] = str(int(float(fico)))
        except (TypeError, ValueError):
            pass

    if base_loan > 0:
        params["baseLoanAmount"] = round(base_loan, 2)
    if purchase_price > 0:
        params["purchasePriceOrEstimatedValue"] = round(purchase_price, 2)

    if addr["addressLineText"]:
        params["subjectProperty_addressLineText"] = addr["addressLineText"]
    if addr["city"]:
        params["subjectProperty_city"] = addr["city"]
    if addr["state"]:
        params["subjectProperty_state"] = addr["state"]
    if addr["county"]:
        params["subjectProperty_county"] = addr["county"]
    if addr["postalCode"]:
        params["subjectProperty_postalCode"] = addr["postalCode"]

    phone = _coerce_phone10(borrower.get("phone"))
    if phone:
        params["borrower1_mobilePhone10digit"] = phone

    monthly_income = _coerce_money(borrower.get("monthly_income")) or 0.0
    if monthly_income <= 0:
        annual = _coerce_money(borrower.get("annual_income")) or 0.0
        if annual > 0:
            monthly_income = annual / 12.0
    if monthly_income > 0:
        params["borrower1_totalMonthlyIncome"] = round(monthly_income, 2)

    return params


# ── Execute ──────────────────────────────────────────────────────────────────

def create_loan_in_arive(
    prequal: dict[str, Any],
    extracted_psa_fields: dict[str, Any],
    *,
    correlation_id: str = "",
) -> dict[str, Any]:
    """
    Fire arive.create_loan via Zapier MCP. Returns a status dict:
        {
          "ok":            bool,
          "status":        "sent" | "skipped:..." | "failed:...",
          "arive_loan_id": str,       # extracted from the response when present
          "params_sent":   dict,      # so the caller can audit what we sent
          "response":      dict|None, # raw Zapier MCP response
        }
    """
    client = ZapierMCPClient()
    if not client.configured:
        return {
            "ok":            False,
            "status":        "skipped:zapier_mcp_not_configured",
            "arive_loan_id": "",
            "params_sent":   {},
            "response":      None,
        }

    params = map_to_arive_params(prequal, extracted_psa_fields)

    try:
        result = client.execute(
            app=ARIVE_APP,
            action=ARIVE_ACTION_CREATE_LOAN,
            mode="write",
            params=params,
            instructions=(
                "Create a new loan file in Arive for the borrower's executed PSA. "
                "This is the LO's next action after PSA receipt — Arive's standard "
                "workflow will send the borrower the 1003 invitation. "
                + (f"correlation_id={correlation_id}" if correlation_id else "")
            ),
            output="Return the Arive loan ID, any borrower-invitation URL, and the loan status.",
        )
    except Exception as e:
        return {
            "ok":            False,
            "status":        f"failed:{type(e).__name__}: {str(e)[:200]}",
            "arive_loan_id": "",
            "params_sent":   params,
            "response":      None,
        }

    arive_loan_id = _extract_arive_loan_id(result)
    return {
        "ok":            not (isinstance(result, dict) and result.get("isError")),
        "status":        "sent",
        "arive_loan_id": arive_loan_id,
        "params_sent":   params,
        "response":      result,
    }


_LOAN_ID_PATTERNS = [
    re.compile(r'"loan_?[Ii]d"\s*:\s*"?([A-Za-z0-9_-]{4,})"?'),
    re.compile(r'"id"\s*:\s*"?([A-Za-z0-9_-]{4,})"?'),
]


def _extract_arive_loan_id(result: Any) -> str:
    """Best-effort: scan the MCP response for an Arive loan id."""
    if not isinstance(result, dict):
        return ""
    content = result.get("content") or []
    for block in content:
        if not isinstance(block, dict):
            continue
        txt = block.get("text") or ""
        for pat in _LOAN_ID_PATTERNS:
            m = pat.search(txt)
            if m:
                return m.group(1)
    return ""
