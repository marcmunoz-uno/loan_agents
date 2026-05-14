"""
loan_officer/lender_router.py — Routes a loan to the right product.

Rules-based for now; will be replaced by an ML ranking model in v2.

Public API:
    from loan_officer.lender_router import suggest_product, score_all_products

    result = suggest_product(borrower, property_)
    # -> {"product": "dscr", "score": 82.5, "rationale": "...", "lenders": [...]}
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from shared.schemas import BorrowerProfile, PropertyProfile
from loan_officer.lender_partners import lenders_for_product


@dataclass
class ProductScore:
    product: str
    display_name: str
    fit_score: float        # 0–100
    qualifies: bool
    disqualifiers: list[str]
    rationale: str
    lenders: list[str]      # lender slugs


# ── Product definitions ────────────────────────────────────────────────────────

PRODUCT_DISPLAY_NAMES = {
    "dscr": "DSCR Loan",
    "fix_flip": "Fix & Flip (Hard Money)",
    "brrrr": "BRRRR Refi",
    "conventional": "Conventional Investment Loan",
    "hard_money": "Hard Money",
    "private": "Private Money",
}


def _score_dscr(b: BorrowerProfile, p: PropertyProfile) -> ProductScore:
    disqualifiers: list[str] = []
    fit = 0.0

    # Must be a rental / income-producing property
    if p.monthly_rent <= 0:
        disqualifiers.append("No projected monthly rent provided")
    if p.property_type not in (
        "single_family", "multi_family_2_4", "multifamily_5plus", "condo", "townhouse"
    ):
        disqualifiers.append(f"Property type '{p.property_type}' not eligible for DSCR")

    # Down payment / LTV
    if b.down_payment_pct < 20 and p.purchase_price > 0:
        disqualifiers.append("DSCR loans require at least 20% down payment")
    else:
        fit += 25

    # Credit
    credit = b.credit_score or 0
    if credit > 0 and credit < 620:
        disqualifiers.append(f"Credit score {credit} is below the 620 minimum for DSCR")
    elif credit >= 680:
        fit += 30
    elif credit >= 640:
        fit += 18

    # Goal alignment — buy-and-hold is ideal
    if b.loan_purpose in ("purchase", "refinance"):
        fit += 20
    if b.loan_purpose == "purchase":
        fit += 5

    # Reserves
    if b.liquidity > 0 and p.purchase_price > 0:
        reserve_ratio = b.liquidity / (p.purchase_price * 0.75)
        if reserve_ratio >= 0.06:  # ~2+ months reserves
            fit += 20

    qualifies = len(disqualifiers) == 0
    lenders = [l["slug"] for l in lenders_for_product("dscr")]

    return ProductScore(
        product="dscr",
        display_name=PRODUCT_DISPLAY_NAMES["dscr"],
        fit_score=round(min(fit, 100), 1),
        qualifies=qualifies,
        disqualifiers=disqualifiers,
        rationale="Best for stabilized rental properties with solid cash flow. Qualification is based on the property's income, not your personal income.",
        lenders=lenders,
    )


def _score_fix_flip(b: BorrowerProfile, p: PropertyProfile) -> ProductScore:
    disqualifiers: list[str] = []
    fit = 0.0

    if p.rehab_budget <= 0:
        disqualifiers.append("Fix & flip requires a rehab budget — provide cost estimate and ARV")
    if p.arv <= 0:
        disqualifiers.append("Fix & flip requires an ARV (after-repair value) estimate")

    # ARV spread
    if p.arv > 0 and p.purchase_price > 0:
        total_cost = p.purchase_price + p.rehab_budget
        arv_ltv = total_cost / p.arv
        if arv_ltv <= 0.65:
            fit += 40
        elif arv_ltv <= 0.70:
            fit += 30
        elif arv_ltv <= 0.75:
            fit += 15
        else:
            disqualifiers.append(f"Total cost ({arv_ltv:.0%} of ARV) exceeds most lender maximums of 70–75%")

    credit = b.credit_score or 0
    if credit > 0 and credit < 620:
        disqualifiers.append(f"Credit score {credit} too low for most hard money lenders (min 620)")
    elif credit >= 660:
        fit += 30
    elif credit >= 620:
        fit += 15

    # Experience matters more for fix & flip
    if b.properties_owned >= 3:
        fit += 20
    elif b.properties_owned >= 1:
        fit += 10
    else:
        fit += 0  # first-timers get no experience bonus

    # Liquidity for rehab draws + holding costs
    if b.liquidity >= p.rehab_budget * 0.30:
        fit += 10

    qualifies = len(disqualifiers) == 0
    lenders = [l["slug"] for l in lenders_for_product("fix_flip")]

    return ProductScore(
        product="fix_flip",
        display_name=PRODUCT_DISPLAY_NAMES["fix_flip"],
        fit_score=round(min(fit, 100), 1),
        qualifies=qualifies,
        disqualifiers=disqualifiers,
        rationale="Short-term bridge loan for rehab + resale. Qualification based on ARV and deal spread, not income.",
        lenders=lenders,
    )


def _score_brrrr(b: BorrowerProfile, p: PropertyProfile) -> ProductScore:
    disqualifiers: list[str] = []
    fit = 0.0

    if b.loan_purpose not in ("purchase", "refinance"):
        disqualifiers.append("BRRRR strategy requires a purchase or refi intent")
    if p.rehab_budget <= 0:
        disqualifiers.append("BRRRR requires a rehab budget (buy distressed, rehab, then refi)")
    if p.monthly_rent <= 0:
        disqualifiers.append("BRRRR requires projected post-rehab rent for the DSCR refi phase")

    credit = b.credit_score or 0
    if credit >= 640:
        fit += 30
    elif credit > 0 and credit < 620:
        disqualifiers.append(f"Credit score {credit} is below the 620 minimum for BRRRR refi")

    if p.arv > 0 and p.purchase_price > 0:
        spread = (p.arv - p.purchase_price - p.rehab_budget) / p.arv
        if spread >= 0.25:
            fit += 35
            # Good equity position for cash-out refi
        elif spread >= 0.20:
            fit += 20
        elif spread > 0:
            fit += 8

    if b.properties_owned >= 2:
        fit += 20
    elif b.properties_owned >= 1:
        fit += 10

    if b.liquidity >= (p.purchase_price + p.rehab_budget) * 0.25:
        fit += 15

    qualifies = len(disqualifiers) == 0
    lenders = [l["slug"] for l in lenders_for_product("brrrr")]

    return ProductScore(
        product="brrrr",
        display_name=PRODUCT_DISPLAY_NAMES["brrrr"],
        fit_score=round(min(fit, 100), 1),
        qualifies=qualifies,
        disqualifiers=disqualifiers,
        rationale="Two-stage: hard money acquisition + rehab, then DSCR refi after property stabilizes. Best for forcing equity.",
        lenders=lenders,
    )


def _score_conventional(b: BorrowerProfile, p: PropertyProfile) -> ProductScore:
    disqualifiers: list[str] = []
    fit = 0.0

    if p.property_type not in ("single_family", "multi_family_2_4", "condo", "townhouse"):
        disqualifiers.append(f"Property type '{p.property_type}' not eligible for conventional investment loan")

    credit = b.credit_score or 0
    if credit < 680:
        disqualifiers.append(f"Credit score {credit} is below the 680 minimum for conventional investment loans")
    elif credit >= 740:
        fit += 35
    elif credit >= 700:
        fit += 25
    elif credit >= 680:
        fit += 15

    if b.annual_income > 0:
        fit += 20

    if b.down_payment_pct >= 25:
        fit += 25
    elif b.down_payment_pct >= 20:
        fit += 15
    elif b.down_payment_pct >= 15:
        fit += 5
    else:
        disqualifiers.append("Conventional investment loans require at least 15% down")

    if b.properties_owned <= 4:
        fit += 10  # Fannie/Freddie limit is 10 financed properties
    else:
        disqualifiers.append(f"With {b.properties_owned} financed properties, conventional may not be available (Fannie/Freddie limit is 10)")

    qualifies = len(disqualifiers) == 0
    lenders: list[str] = []  # Conventional loans go through retail mortgage lenders, not in our partner network

    return ProductScore(
        product="conventional",
        display_name=PRODUCT_DISPLAY_NAMES["conventional"],
        fit_score=round(min(fit, 100), 1),
        qualifies=qualifies,
        disqualifiers=disqualifiers,
        rationale="Fannie/Freddie-backed investment loan. Best rates but requires strong credit and income documentation.",
        lenders=lenders,
    )


def _score_private(b: BorrowerProfile, p: PropertyProfile) -> ProductScore:
    """Private money is always available as a fallback for unique situations."""
    fit = 30.0  # baseline — private money can often be arranged
    rationale = "Private capital for deals that don't fit standard boxes (land, distressed commercial, complex structures)."

    if p.property_type in ("commercial", "land", "mixed_use"):
        fit += 30

    return ProductScore(
        product="private",
        display_name=PRODUCT_DISPLAY_NAMES["private"],
        fit_score=fit,
        qualifies=True,
        disqualifiers=[],
        rationale=rationale,
        lenders=[],
    )


# ── Public router functions ───────────────────────────────────────────────────

def score_all_products(
    borrower: BorrowerProfile, property_: PropertyProfile
) -> list[ProductScore]:
    """Score all loan products for a given borrower+property. Returns sorted list."""
    scorers = [_score_dscr, _score_fix_flip, _score_brrrr, _score_conventional, _score_private]
    scores = [f(borrower, property_) for f in scorers]
    # Sort: qualifying products first, then by fit score descending
    return sorted(scores, key=lambda s: (not s.qualifies, -s.fit_score))


def suggest_product(
    borrower: BorrowerProfile, property_: PropertyProfile
) -> dict[str, Any]:
    """
    Returns the best product recommendation.

    Result shape:
    {
        "product": "dscr",
        "display_name": "DSCR Loan",
        "fit_score": 78.5,
        "rationale": "...",
        "lenders": ["kiavi", "lima_one"],
        "alternatives": [...],
        "all_scores": [...],
    }
    """
    all_scores = score_all_products(borrower, property_)
    best = all_scores[0]

    return {
        "product": best.product,
        "display_name": best.display_name,
        "fit_score": best.fit_score,
        "qualifies": best.qualifies,
        "disqualifiers": best.disqualifiers,
        "rationale": best.rationale,
        "lenders": best.lenders,
        "alternatives": [
            {
                "product": s.product,
                "display_name": s.display_name,
                "fit_score": s.fit_score,
                "qualifies": s.qualifies,
            }
            for s in all_scores[1:3]  # top 2 alternatives
        ],
        "all_scores": [
            {
                "product": s.product,
                "display_name": s.display_name,
                "fit_score": s.fit_score,
                "qualifies": s.qualifies,
                "disqualifiers": s.disqualifiers,
            }
            for s in all_scores
        ],
    }
