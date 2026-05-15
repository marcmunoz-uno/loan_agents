"""
loan_processor/condition_generator.py — Generate condition lists per submission.

Produces the set of conditions an underwriter will request for a given product type
and lender. Conditions are categorized by type and severity (PTSU / PTC / PTF).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Condition:
    """A single underwriting condition."""
    condition_type: str   # doc_request | clarification | escrow_holdback | repair_required
    severity: str         # prior_to_submission | prior_to_close | funding
    description: str
    lender_specific: str = ""  # lender_id that flagged this, or "all"
    required: bool = True


def generate_conditions(
    product_type: str,
    lender_id: str,
    loan_amount: float = 0,
    fico: int = 0,
    ltv: float = 0,
    dscr: Optional[float] = None,
    property_type: str = "SFR",
    is_occupied: bool = False,
    is_entity_vesting: bool = False,
    is_short_term_rental: bool = False,
    is_condo: bool = False,
) -> list[Condition]:
    """
    Generate the conditions list for a given product + lender combination.

    Returns conditions sorted by severity (PTSU first, then PTC, then PTF).
    """
    conditions: list[Condition] = []

    if product_type in ("dscr", "brrrr"):
        conditions.extend(_dscr_conditions(
            lender_id=lender_id,
            loan_amount=loan_amount,
            fico=fico,
            ltv=ltv,
            dscr=dscr,
            is_occupied=is_occupied,
            is_entity_vesting=is_entity_vesting,
            is_short_term_rental=is_short_term_rental,
            is_condo=is_condo,
        ))

    if product_type in ("fix_flip", "brrrr"):
        conditions.extend(_fix_flip_conditions(
            lender_id=lender_id,
            is_entity_vesting=is_entity_vesting,
        ))

    if product_type == "brrrr":
        conditions.extend(_brrrr_specific_conditions(lender_id=lender_id))

    # Sort: PTSU first, then PTC, then funding
    order = {"prior_to_submission": 0, "prior_to_close": 1, "funding": 2}
    conditions.sort(key=lambda c: order.get(c.severity, 99))
    return conditions


# ─────────────────────────────────────────────────────────────────────────────
# DSCR condition set
# ─────────────────────────────────────────────────────────────────────────────

def _dscr_conditions(
    lender_id: str,
    loan_amount: float,
    fico: int,
    ltv: float,
    dscr: Optional[float],
    is_occupied: bool,
    is_entity_vesting: bool,
    is_short_term_rental: bool,
    is_condo: bool,
) -> list[Condition]:
    conds: list[Condition] = []

    # ── Prior to Submission ──────────────────────────────────────────────────

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_submission",
        description="Tri-merge credit report (lender will order — do not submit broker-pulled credit)",
        lender_specific=lender_id,
    ))

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_submission",
        description="Executed purchase contract or payoff statement (refinance)",
        lender_specific="all",
    ))

    if is_entity_vesting:
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_submission",
            description="LLC operating agreement (all pages, including executed amendments)",
            lender_specific="all",
        ))
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_submission",
            description="EIN letter (IRS Form SS-4 or CP-575)",
            lender_specific="all",
        ))
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_submission",
            description="Certificate of Good Standing from state of entity formation (must be < 90 days old)",
            lender_specific="all",
        ))

    # ── Prior to Close ───────────────────────────────────────────────────────

    # Appraisal — AVM waiver check
    use_avm = (
        loan_amount > 0 and loan_amount <= 400_000
        and fico >= 720
        and ltv <= 0.70
        and not is_condo
        and not is_short_term_rental
        and lender_id in ("lima_one_dscr", "kiavi_dscr")
    )

    if use_avm:
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_close",
            description=(
                "AVM (automated valuation) — eligible given loan ≤ $400K, FICO ≥ 720, LTV ≤ 70%. "
                "Lender waiver required. If AVM is not approved, full DSCR appraisal with Form 1007."
            ),
            lender_specific=lender_id,
        ))
    else:
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_close",
            description=(
                "DSCR appraisal — full field review with market rent schedule (Form 1007). "
                "Appraiser must be on lender's approved AMC list."
            ),
            lender_specific=lender_id,
        ))

    # Rent schedule / lease
    if is_occupied:
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_close",
            description=(
                "Executed lease agreement (all pages + any addenda). "
                "If tenant is month-to-month, confirm landlord's right to modify rent for DSCR calculation."
            ),
            lender_specific="all",
        ))
        conds.append(Condition(
            condition_type="clarification",
            severity="prior_to_close",
            description=(
                "Tenant estoppel or rent verification letter (if current lease is > 12 months old or unsigned by all parties)"
            ),
            lender_specific="all",
            required=False,
        ))
    else:
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_close",
            description=(
                "Market rent letter or Form 1007 from appraiser (property vacant — proforma rent, not actual). "
                "NOTE: DSCR is calculated on proforma rent. If appraised market rent is lower than projected, DSCR may fall below floor."
            ),
            lender_specific="all",
        ))

    # Short-term rental
    if is_short_term_rental:
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_close",
            description=(
                "12-month rental income history from platform (Airbnb/VRBO host report) OR "
                "STR market analysis from licensed appraiser. "
                "Lender will haircut gross rental income to 75% for DSCR calculation."
            ),
            lender_specific=lender_id,
        ))

    # Reserves
    reserves_months = 6 if lender_id in ("lima_one_dscr", "roc_capital_dscr") else 3
    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_close",
        description=(
            f"Bank statements — 2 most recent months, all pages. "
            f"Must demonstrate {reserves_months} months PITI in reserves after closing. "
            f"Sources: personal/business accounts (100%), retirement accounts (60% haircut). "
            f"Gift funds NOT acceptable for reserves."
        ),
        lender_specific=lender_id,
    ))

    # Insurance
    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_close",
        description=(
            "Insurance binder — landlord/dwelling policy. Replacement cost must be ≥ loan amount. "
            "Carrier must be rated A- or better (AM Best). Lender must appear as additional insured / mortgagee."
        ),
        lender_specific="all",
    ))

    # Title
    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_close",
        description=(
            "Title commitment — preliminary title report showing clean title. "
            "Any open liens, judgments, or encumbrances must be cleared or subordinated."
        ),
        lender_specific="all",
    ))

    # Property taxes
    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_close",
        description=(
            "Property tax confirmation — taxes must be current with no delinquencies. "
            "Provide county tax receipt or lender will pull at closing."
        ),
        lender_specific="all",
    ))

    # Condo questionnaire
    if is_condo:
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_close",
            description=(
                "Condo questionnaire from HOA — required within 90 days. Allow 10–15 days for HOA response. "
                "Order immediately. Lender will review for litigation, delinquency rate (>15% is ineligible), and insurance."
            ),
            lender_specific=lender_id,
        ))

    # ── Funding ──────────────────────────────────────────────────────────────

    conds.append(Condition(
        condition_type="doc_request",
        severity="funding",
        description="Final title commitment and lender-required title endorsements",
        lender_specific="all",
    ))

    conds.append(Condition(
        condition_type="doc_request",
        severity="funding",
        description="Evidence of insurance with lender named as mortgagee (ATIMA clause)",
        lender_specific="all",
    ))

    return conds


# ─────────────────────────────────────────────────────────────────────────────
# Fix & Flip condition set
# ─────────────────────────────────────────────────────────────────────────────

def _fix_flip_conditions(lender_id: str, is_entity_vesting: bool) -> list[Condition]:
    conds: list[Condition] = []

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_submission",
        description=(
            "Renovation budget — line-item breakdown by trade "
            "(demo, framing, roofing, electrical, plumbing, HVAC, finishes, etc.). "
            "Must total to within 10% of scope of work or UW will request revision."
        ),
        lender_specific=lender_id,
    ))

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_submission",
        description=(
            "General contractor information — company name, state license number, and certificate of insurance. "
            "GC must be licensed in the property state. Unlicensed GC = automatic stall at PTSU."
        ),
        lender_specific=lender_id,
    ))

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_submission",
        description=(
            "Borrower experience documentation — prior project portfolio, closed HUDs showing purchase and resale, "
            "or letter from CPA/title company confirming completed projects. "
            "Experience tier determines max LTV/LTC."
        ),
        lender_specific=lender_id,
    ))

    if is_entity_vesting:
        conds.append(Condition(
            condition_type="doc_request",
            severity="prior_to_submission",
            description="LLC operating agreement + EIN letter + Certificate of Good Standing",
            lender_specific=lender_id,
        ))

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_close",
        description=(
            "AS-IS appraisal AND ARV (after-repair value) appraisal. "
            "Lender orders both. LTV calculated on ARV is the binding cap. "
            "Do not order your own appraisal — lender will not accept it."
        ),
        lender_specific=lender_id,
    ))

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_close",
        description=(
            "Builder's risk insurance (or landlord policy with renovation endorsement). "
            "Coverage must extend through expected renovation completion date."
        ),
        lender_specific=lender_id,
    ))

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_close",
        description=(
            "Exit strategy documentation — purchase contract (wholesale/retail buyer) "
            "or refinance analysis showing DSCR ≥ 1.00 at projected stabilized rents."
        ),
        lender_specific=lender_id,
    ))

    conds.append(Condition(
        condition_type="doc_request",
        severity="prior_to_close",
        description=(
            "Draw Schedule Agreement — signed by borrower and GC. "
            "Defines draw milestones, inspection requirements, and lien waiver process."
        ),
        lender_specific=lender_id,
    ))

    return conds


# ─────────────────────────────────────────────────────────────────────────────
# BRRRR-specific additions
# ─────────────────────────────────────────────────────────────────────────────

def _brrrr_specific_conditions(lender_id: str) -> list[Condition]:
    return [
        Condition(
            condition_type="clarification",
            severity="prior_to_submission",
            description=(
                "Exit strategy: confirm borrower intends to refinance into DSCR product at stabilization "
                "(property leased, DSCR ≥ 1.00). Provide signed exit strategy letter."
            ),
            lender_specific=lender_id,
        ),
        Condition(
            condition_type="doc_request",
            severity="prior_to_close",
            description=(
                "Stabilization plan — timeline from acquisition to lease-up. "
                "Lender will review to confirm BRRRR refi is achievable within 12-month loan term."
            ),
            lender_specific=lender_id,
        ),
    ]
