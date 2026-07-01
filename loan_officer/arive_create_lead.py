"""
loan_officer/arive_create_lead.py — Create a *lead* in Arive from a Typeform intake.

This is the top-of-funnel counterpart to arive_create_loan.py: a qualified
Typeform submission becomes an Arive **lead** (not a loan), so it does NOT
trigger Arive's 1003 borrower invitation. Leads are promoted to loans later
(Convert Lead to Loan) once the borrower is under contract.

⚠️ KNOWN BLOCKER (2026-06): the Arive Zapier app's `create_lead` action is
broken — it rejects the borrower fields (`borrower.0.firstName should not be
empty`) whether they're sent as `borrower_*` or `borrower1_*`, so nothing
actually lands. The mapping + wiring here are complete and tested; firing is
gated behind ARIVE_CREATE_LEADS=1 (default OFF) so we don't hammer a broken
action. Flip the flag on once Arive/Zapier fixes the action.
See memory: reference_arive_zapier_create_lead_bug.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from shared.zapier_mcp import ZapierMCPClient

ARIVE_APP = "arive"
ARIVE_ACTION_CREATE_LEAD = "create_lead"

DEFAULT_ORIGINATOR_EMAIL = os.environ.get("LO_SIGNER_EMAIL", "marc@munoz.ltd")

# Typeform "primary_residence_status" (normalised in the mapper) → Arive occupancy.
_OCCUPANCY_MAP = {"own": "Own", "rent": "Rent", "living_rent_free": "LivingRentFree"}

# Arive Lead Source choice id for "Social Media" (Typeform/IG funnels).
_LEAD_SOURCE_SOCIAL = "68605"


def _enabled() -> bool:
    return os.environ.get("ARIVE_CREATE_LEADS", "").strip() in ("1", "true", "yes")


def _coerce_phone10(value: Any) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    return digits[-10:] if len(digits) >= 10 else ""


def map_intake_to_lead_params(
    intake: dict[str, Any],
    *,
    originator_email: Optional[str] = None,
) -> dict[str, Any]:
    """
    Map a Typeform intake dict (see typeform.mapper.map_payload) → Arive
    `create_lead` params. Only non-empty values are included so Arive's
    optional defaults apply. Uses the action's documented `borrower_*` field
    names + DSCR-investor defaults (Non-QM / Investment).
    """
    first = (intake.get("first_name") or "").strip() or "Borrower"
    last = (intake.get("last_name") or "").strip() or first or "Unknown"

    params: dict[str, Any] = {
        # ── Required ─────────────────────────────────────────────────────────
        "loanPurpose":               "Purchase",
        "assigneeEmail":             originator_email or DEFAULT_ORIGINATOR_EMAIL,
        "borrower_firstName":        first,
        "borrower_lastName":         last,
        "borrower_emailAddressText": str(intake.get("email") or ""),
        # ── DSCR-investor defaults ───────────────────────────────────────────
        "mortgageType":              "NonQM",
        "propertyUsageType":         "Investment",
        "leadStatus":                "NEW",
        "leadSource":                _LEAD_SOURCE_SOCIAL,
        # ── Provenance ───────────────────────────────────────────────────────
        "crmReferenceId":            str(intake.get("typeform_response_id") or ""),
    }

    fico = intake.get("credit_score_estimate")
    if fico:
        try:
            params["estimatedFICO"] = str(int(float(fico)))
        except (TypeError, ValueError):
            pass

    phone = _coerce_phone10(intake.get("phone"))
    if phone:
        params["borrower_mobilePhone10digit"] = phone

    occ = _OCCUPANCY_MAP.get((intake.get("primary_residence_status") or "").lower())
    if occ:
        params["borrower_occupancy"] = occ

    return params


def create_lead_from_intake(
    intake: dict[str, Any],
    *,
    correlation_id: str = "",
) -> dict[str, Any]:
    """
    Best-effort: create an Arive lead from a Typeform intake. Never raises —
    returns a status dict the caller can log. Gated behind ARIVE_CREATE_LEADS
    (default OFF) so the known-broken Arive action isn't hit until it's fixed.

    Returns:
        {"ok": bool, "status": "sent"|"skipped:<reason>"|"failed:<err>",
         "arive_lead_id": str, "params_sent": dict}
    """
    if not _enabled():
        return {"ok": False, "status": "skipped:disabled", "arive_lead_id": "", "params_sent": {}}

    client = ZapierMCPClient()
    if not client.configured:
        return {"ok": False, "status": "skipped:zapier_mcp_not_configured",
                "arive_lead_id": "", "params_sent": {}}

    params = map_intake_to_lead_params(intake)
    try:
        result = client.execute(
            app=ARIVE_APP,
            action=ARIVE_ACTION_CREATE_LEAD,
            mode="write",
            params=params,
            instructions=(
                "Create a new top-of-funnel lead in Arive for a Non-QM DSCR "
                "investor who submitted the qualification Typeform. Do NOT create "
                "a loan or send the borrower a 1003 invitation. "
                + (f"correlation_id={correlation_id}" if correlation_id else "")
            ),
            output="Return the Arive lead ID and status.",
        )
    except Exception as e:  # noqa: BLE001 — best-effort, never break the webhook
        return {"ok": False, "status": f"failed:{type(e).__name__}: {str(e)[:200]}",
                "arive_lead_id": "", "params_sent": params}

    is_error = isinstance(result, dict) and result.get("isError")
    return {
        "ok": not is_error,
        "status": "failed:arive_rejected" if is_error else "sent",
        "arive_lead_id": _extract_lead_id(result),
        "params_sent": params,
    }


_LEAD_ID_PATTERNS = [
    re.compile(r'"lead_?[Ii]d"\s*:\s*"?([A-Za-z0-9_-]{4,})"?'),
    re.compile(r'"id"\s*:\s*"?([A-Za-z0-9_-]{4,})"?'),
]


def _extract_lead_id(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for block in result.get("content") or []:
        if isinstance(block, dict):
            txt = block.get("text") or ""
            for pat in _LEAD_ID_PATTERNS:
                m = pat.search(txt)
                if m:
                    return m.group(1)
    return ""
