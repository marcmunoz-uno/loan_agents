"""
loan_processor/pre_underwriting.py — Core pre-underwriting engine.

Entry point: pre_underwrite(application_id) -> PreUnderwritingReport

Pulls the application + prequal from the DB, runs the deal against the
guidelines matrix, generates conditions, red flags, and a credit memo draft.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db import get_conn, fetchone
from loan_processor.guideline_engine import get_engine
from loan_processor.condition_generator import generate_conditions, Condition
from loan_processor.credit_memo import draft_credit_memo

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RedFlag:
    flag_type: str          # fico_below_min|ltv_above_max|property_ineligible|dscr_too_low|reserves_short|cashflow_negative
    severity: str           # deal_killer | significant | minor
    description: str
    mitigation_suggestion: str = ""


@dataclass
class PreUnderwritingReport:
    application_id: str
    summary: str                          # one-liner
    overall_status: str                   # clean | conditional | decline_risk
    lender_fit: list[dict[str, Any]] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    red_flags: list[RedFlag] = field(default_factory=list)
    computed_metrics: dict[str, Any] = field(default_factory=dict)
    suggested_lender: str = ""
    credit_memo_draft: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def pre_underwrite(application_id: str) -> PreUnderwritingReport:
    """
    Run full pre-underwriting check for an application.

    Reads from the database, runs against the guideline engine,
    and returns a PreUnderwritingReport (does not write to DB — caller saves).
    """
    # ── Load application + prequal from DB ───────────────────────────────────
    with get_conn() as conn:
        app = fetchone(conn, "SELECT * FROM loan_applications WHERE id = ?", (application_id,))
        if not app:
            raise ValueError(f"Application not found: {application_id}")
        prequal = fetchone(conn, "SELECT * FROM loan_prequals WHERE id = ?", (app["prequal_id"],))

    if not prequal:
        raise ValueError(f"Prequal not found for application {application_id}")

    borrower_data = _load_json(prequal["borrower_data"])
    property_data = _load_json(prequal["property_data"])
    product_type = prequal.get("suggested_product", "dscr")

    # ── Compute key metrics ──────────────────────────────────────────────────
    metrics = _compute_metrics(borrower_data, property_data, prequal, product_type)

    # ── Run guideline engine ─────────────────────────────────────────────────
    engine = get_engine()
    state = _extract_state(property_data.get("address", ""))
    lender_matches = engine.match_lenders(
        fico=metrics["fico"],
        ltv=metrics["ltv"],
        product_type=product_type,
        dscr=metrics.get("dscr"),
        state=state,
        loan_amount=metrics["loan_amount"],
        property_type=property_data.get("property_type", "single_family"),
        loan_purpose=borrower_data.get("loan_purpose", "purchase"),
    )

    qualifying = [m for m in lender_matches if m["qualifies"]]
    near_miss = [m for m in lender_matches if not m["qualifies"] and m["fit_score"] >= 50]

    # ── Overall status ───────────────────────────────────────────────────────
    if not qualifying and not near_miss:
        overall_status = "decline_risk"
    elif not qualifying:
        overall_status = "decline_risk"
    else:
        # Check if all qualifying lenders have no critical conditions
        overall_status = "clean" if _is_clean(metrics, qualifying) else "conditional"

    # ── Red flags ────────────────────────────────────────────────────────────
    red_flags = _generate_red_flags(metrics, lender_matches, product_type)

    # ── Suggested lender ─────────────────────────────────────────────────────
    suggested_lender = qualifying[0]["lender_id"] if qualifying else ""
    suggested_lender_name = qualifying[0]["lender"] if qualifying else "No eligible lender"

    # ── Conditions list ──────────────────────────────────────────────────────
    conditions: list[Condition] = []
    if suggested_lender:
        is_entity = bool(
            borrower_data.get("entity_name") or
            property_data.get("vesting_entity")
        )
        is_occupied = property_data.get("monthly_rent", 0) > 0
        is_condo = property_data.get("property_type", "") in ("condo", "condo_warrantable")
        is_str = property_data.get("is_short_term_rental", False)

        conditions = generate_conditions(
            product_type=product_type,
            lender_id=suggested_lender,
            loan_amount=metrics["loan_amount"],
            fico=metrics["fico"],
            ltv=metrics["ltv"],
            dscr=metrics.get("dscr"),
            property_type=property_data.get("property_type", "single_family"),
            is_occupied=is_occupied,
            is_entity_vesting=is_entity,
            is_short_term_rental=is_str,
            is_condo=is_condo,
        )

    # ── Credit memo ──────────────────────────────────────────────────────────
    conditions_summary = [c.description for c in conditions]
    credit_memo = draft_credit_memo(
        borrower_data=borrower_data,
        property_data=property_data,
        computed_metrics=metrics,
        suggested_lender=suggested_lender_name,
        overall_status=overall_status,
        conditions_summary=conditions_summary,
        lender_id=suggested_lender or None,
    )

    # ── Summary line ─────────────────────────────────────────────────────────
    cond_count = len([c for c in conditions if c.severity == "prior_to_submission"])
    if overall_status == "clean":
        summary = f"Clean file for {suggested_lender_name} — {cond_count} PTSU condition(s)"
    elif overall_status == "conditional":
        flag_count = len([f for f in red_flags if f.severity in ("significant", "deal_killer")])
        summary = f"Conditional — {suggested_lender_name} possible with {flag_count} flag(s) to address"
    else:
        summary = f"Decline risk — {len(red_flags)} red flag(s), no standard qualifying lenders"
        if near_miss:
            summary += f" (nearest: {near_miss[0]['lender']} at {near_miss[0]['fit_score']}% fit)"

    return PreUnderwritingReport(
        application_id=application_id,
        summary=summary,
        overall_status=overall_status,
        lender_fit=lender_matches[:8],   # top 8
        conditions=conditions,
        red_flags=red_flags,
        computed_metrics=metrics,
        suggested_lender=suggested_lender_name,
        credit_memo_draft=credit_memo,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Metric computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_metrics(
    b: dict, p: dict, prequal: dict, product_type: str
) -> dict[str, Any]:
    purchase_price = p.get("purchase_price", 0) or 0
    estimated_value = p.get("estimated_value", 0) or purchase_price
    monthly_rent = p.get("monthly_rent", 0) or 0
    annual_taxes = p.get("annual_taxes", 0) or 0
    annual_insurance = p.get("annual_insurance", 0) or 0
    hoa_monthly = p.get("hoa_monthly", 0) or 0
    rehab_budget = p.get("rehab_budget", 0) or 0
    arv = p.get("arv", 0) or 0

    down_pct = b.get("down_payment_pct", 0) or 0
    desired_loan = b.get("desired_loan_amount", 0) or 0

    # Loan amount
    if desired_loan > 0:
        loan_amount = desired_loan
    elif down_pct > 0 and purchase_price > 0:
        loan_amount = purchase_price * (1 - down_pct / 100)
    else:
        loan_amount = purchase_price * 0.75

    loan_amount = round(loan_amount, 2)

    # LTV
    prop_value = estimated_value or purchase_price
    ltv = round(loan_amount / prop_value, 4) if prop_value > 0 else 0.0

    # LTC (fix & flip)
    total_cost = purchase_price + rehab_budget
    ltc = round(loan_amount / total_cost, 4) if total_cost > 0 else 0.0

    # Monthly PITI
    monthly_taxes = round(annual_taxes / 12, 2)
    monthly_insurance = round(annual_insurance / 12, 2)
    monthly_pni = round(prequal.get("monthly_payment_estimate", 0) or 0, 2)
    monthly_piti = round(monthly_pni + monthly_taxes + monthly_insurance + hoa_monthly, 2)

    # DSCR
    dscr = None
    monthly_cashflow = None
    dscr_coverage_gap = None
    if product_type in ("dscr", "brrrr") and monthly_piti > 0 and monthly_rent > 0:
        dscr = round(monthly_rent / monthly_piti, 3)
        monthly_cashflow = round(monthly_rent - monthly_piti, 2)
        # Gap to 1.10 target (Lima One / common floor)
        dscr_coverage_gap = round(dscr - 1.10, 3)

    # Use prequal's computed DSCR if we couldn't compute it
    if dscr is None:
        dscr = prequal.get("dscr")

    fico = b.get("credit_score") or 0

    return {
        "fico": int(fico),
        "loan_amount": loan_amount,
        "ltv": ltv,
        "ltc": ltc,
        "monthly_pni": monthly_pni,
        "monthly_piti": monthly_piti,
        "monthly_cashflow": monthly_cashflow,
        "dscr": dscr,
        "dscr_coverage_gap": dscr_coverage_gap,
        "monthly_rent": monthly_rent,
        "arv": arv,
        "purchase_price": purchase_price,
        "rehab_budget": rehab_budget,
        "down_pct": down_pct,
        "product_type": product_type,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Red flag generation
# ─────────────────────────────────────────────────────────────────────────────

def _generate_red_flags(
    metrics: dict, lender_matches: list, product_type: str
) -> list[RedFlag]:
    flags: list[RedFlag] = []
    fico = metrics["fico"]
    ltv = metrics["ltv"]
    dscr = metrics.get("dscr")
    loan_amount = metrics["loan_amount"]
    monthly_cashflow = metrics.get("monthly_cashflow")

    # FICO flags
    if fico > 0:
        if fico < 620:
            flags.append(RedFlag(
                flag_type="fico_below_min",
                severity="deal_killer",
                description=f"FICO estimate {fico} is below all standard lender minimums (620 is the floor at LendingOne/New Silver).",
                mitigation_suggestion="Borrower must improve credit score before proceeding. No standard DSCR or fix-and-flip lender will approve below 620.",
            ))
        elif fico < 640:
            flags.append(RedFlag(
                flag_type="fico_below_min",
                severity="significant",
                description=f"FICO estimate {fico} is below Kiavi (640) and Lima One (660) minimums. Only LendingOne (620 min) or New Silver (620 min) options.",
                mitigation_suggestion="File is narrow. Pull tri-merge credit immediately — self-reported FICOs often run 20–40 pts high. If actual FICO is below 620, deal is dead.",
            ))
        elif fico < 660:
            flags.append(RedFlag(
                flag_type="fico_below_min",
                severity="minor",
                description=f"FICO estimate {fico} is below Lima One's 660 minimum. Eligible for Kiavi, LendingOne, and New Silver.",
                mitigation_suggestion="Route to Kiavi (640 min) or LendingOne (620 min). Pull tri-merge before committing to a lender.",
            ))

    # LTV flags
    if ltv > 0.85:
        flags.append(RedFlag(
            flag_type="ltv_above_max",
            severity="deal_killer",
            description=f"LTV {ltv:.0%} exceeds all lender maximum LTVs (max 80% for purchase DSCR).",
            mitigation_suggestion="Borrower needs to increase down payment. At 80% max LTV, need at least 20% down.",
        ))
    elif ltv > 0.80:
        flags.append(RedFlag(
            flag_type="ltv_above_max",
            severity="significant",
            description=f"LTV {ltv:.0%} exceeds standard 80% purchase LTV maximum.",
            mitigation_suggestion="Requires minimum down payment increase. Check if purchase price is negotiable.",
        ))

    # DSCR flags
    if dscr is not None and product_type in ("dscr", "brrrr"):
        if dscr < 0.75:
            flags.append(RedFlag(
                flag_type="dscr_too_low",
                severity="deal_killer",
                description=f"DSCR {dscr:.2f} is severely below minimum. Even no-DSCR products require a viable rent-to-value ratio.",
                mitigation_suggestion="Property cash flow does not support financing at this purchase price. Renegotiate price, identify higher-rent comparable, or decline.",
            ))
        elif dscr < 0.95:
            flags.append(RedFlag(
                flag_type="dscr_too_low",
                severity="deal_killer",
                description=f"DSCR {dscr:.2f} below LendingOne's 0.95 minimum for standard product. Only no-DSCR variant applies.",
                mitigation_suggestion="LendingOne no-DSCR at 65% LTV (+50–75bps). Verify LTV is at or below 65% for this product.",
            ))
        elif dscr < 1.00:
            flags.append(RedFlag(
                flag_type="dscr_too_low",
                severity="significant",
                description=f"DSCR {dscr:.2f} below Kiavi/Lima One/Roc minimums (1.00). Eligible for LendingOne only.",
                mitigation_suggestion="Route to LendingOne (0.95 min). Confirm LTV ≤ 80%. Note: proforma rent must be verified by appraiser.",
            ))
        elif dscr < 1.10:
            flags.append(RedFlag(
                flag_type="dscr_too_low",
                severity="minor",
                description=f"DSCR {dscr:.2f} qualifies at Kiavi/LendingOne (1.00 min) but not Lima One/Roc Capital (1.10 min).",
                mitigation_suggestion="Primary route: Kiavi or LendingOne. Lima One is out unless DSCR improves. Watch proforma risk.",
            ))

    # Negative cash flow flag
    if monthly_cashflow is not None and monthly_cashflow < 0:
        flags.append(RedFlag(
            flag_type="cashflow_negative",
            severity="significant",
            description=f"Monthly cash flow is negative (${monthly_cashflow:,.0f}/mo). Property does not cash flow.",
            mitigation_suggestion="Negative cash flow is not a lender disqualifier by itself (DSCR is what matters), but borrower must demonstrate reserves to cover shortfall. Flag for MLO to discuss with borrower.",
        ))

    # Loan amount flags
    if loan_amount > 0:
        qualifying = [m for m in lender_matches if m["qualifies"]]
        if not qualifying:
            no_dscr_near_miss = any(
                "no-DSCR variant available" in r
                for m in lender_matches
                for r in m.get("qualify_reasons", [])
            )
            if not no_dscr_near_miss:
                flags.append(RedFlag(
                    flag_type="fico_below_min",
                    severity="deal_killer",
                    description="No standard lender in the matrix qualifies this file based on available data.",
                    mitigation_suggestion="Review all decline reasons per lender. Consider if a hard money or private lender option exists outside the standard stack.",
                ))

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_clean(metrics: dict, qualifying: list) -> bool:
    """Return True if top qualifying lender has no significant red flags."""
    dscr = metrics.get("dscr")
    fico = metrics["fico"]
    ltv = metrics["ltv"]
    # Clean means: decent FICO, LTV within range, DSCR above common floor
    if fico < 640:
        return False
    if ltv > 0.80:
        return False
    if dscr is not None and dscr < 1.00:
        return False
    return True


def _extract_state(address: str) -> str:
    """Best-effort extract 2-letter state code from an address string."""
    import re
    # Pattern: "City ST XXXXX" at end, or "City, ST XXXXX"
    match = re.search(r'\b([A-Z]{2})\s*\d{5}', address.upper())
    if match:
        return match.group(1)
    # Try last two-letter word
    tokens = address.split()
    for token in reversed(tokens):
        t = token.strip(",. ").upper()
        if len(t) == 2 and t.isalpha():
            return t
    return ""


def _load_json(v: Any) -> dict:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v or {}
