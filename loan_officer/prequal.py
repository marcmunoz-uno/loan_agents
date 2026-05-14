"""
loan_officer/prequal.py — Pre-qualification logic and scoring.

Computes a 0–100 fit score for a borrower+property combo, calculates key metrics
(DSCR, LTV, monthly payment estimate), and returns strengths/concerns/next_steps.
"""

from __future__ import annotations
from typing import Optional
from shared.schemas import BorrowerProfile, PropertyProfile


# Monthly payment factor tables (approximations — not real amortization tables)
# Used for estimates only. Real payment is computed by the lender.
def _monthly_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    """Standard mortgage payment formula."""
    if annual_rate_pct <= 0 or term_months <= 0 or principal <= 0:
        return 0.0
    r = (annual_rate_pct / 100) / 12
    return principal * (r * (1 + r) ** term_months) / ((1 + r) ** term_months - 1)


def compute_dscr(
    monthly_rent: float,
    monthly_payment: float,
    annual_taxes: float,
    annual_insurance: float,
    hoa_monthly: float = 0.0,
    vacancy_rate: float = 0.05,   # 5% vacancy default
) -> float:
    """
    DSCR = Net Operating Income / Annual Debt Service

    NOI = (gross rent * (1 - vacancy)) - taxes - insurance - HOA
    """
    if monthly_payment <= 0:
        return 0.0
    gross_annual_rent = monthly_rent * 12 * (1 - vacancy_rate)
    noi = gross_annual_rent - annual_taxes - annual_insurance - (hoa_monthly * 12)
    annual_debt_service = monthly_payment * 12
    return round(noi / annual_debt_service, 3) if annual_debt_service > 0 else 0.0


def compute_ltv(loan_amount: float, property_value: float) -> float:
    if property_value <= 0:
        return 0.0
    return round(loan_amount / property_value, 3)


def score_prequal(
    borrower: BorrowerProfile,
    property_: PropertyProfile,
    suggested_product: str,
    dscr: Optional[float] = None,
    ltv: Optional[float] = None,
) -> tuple[float, list[str], list[str]]:
    """
    Returns (score: float 0-100, strengths: list[str], concerns: list[str]).

    Scoring logic (each component contributes up to its weight):
      - Credit score (30 pts)
      - DSCR (30 pts, DSCR products only)
      - LTV / down payment (20 pts)
      - Liquidity (10 pts)
      - Experience (10 pts)
    """
    score = 0.0
    strengths: list[str] = []
    concerns: list[str] = []

    # ── Credit score (30 pts) ─────────────────────────────────────────────────
    credit = borrower.credit_score or 0
    if credit >= 760:
        score += 30
        strengths.append(f"Excellent credit score ({credit}) — qualifies for best rates")
    elif credit >= 720:
        score += 25
        strengths.append(f"Strong credit score ({credit})")
    elif credit >= 680:
        score += 18
    elif credit >= 640:
        score += 10
        concerns.append(f"Credit score ({credit}) is below preferred 680+ for most products")
    elif credit >= 600:
        score += 4
        concerns.append(f"Credit score ({credit}) limits product options — hard money may be the only fit")
    elif credit > 0:
        concerns.append(f"Credit score ({credit}) is below most lender minimums (620)")

    # ── DSCR (30 pts — DSCR/BRRRR products) ─────────────────────────────────
    if suggested_product in ("dscr", "brrrr") and dscr is not None:
        if dscr >= 1.25:
            score += 30
            strengths.append(f"Strong DSCR of {dscr:.2f} — well above the 1.1 minimum")
        elif dscr >= 1.1:
            score += 22
            strengths.append(f"DSCR of {dscr:.2f} meets minimum requirements")
        elif dscr >= 1.0:
            score += 12
            concerns.append(f"DSCR of {dscr:.2f} is thin — lender will scrutinize vacancy and expense assumptions")
        else:
            score += 0
            concerns.append(f"DSCR of {dscr:.2f} is below 1.0 — property does not cash-flow at this price/rate")
    elif suggested_product in ("fix_flip", "hard_money") and property_.arv > 0 and property_.purchase_price > 0:
        rehab_ltv = (property_.purchase_price + property_.rehab_budget) / property_.arv
        if rehab_ltv <= 0.65:
            score += 30
            strengths.append(f"Excellent deal spread: {rehab_ltv:.0%} of ARV — strong hard money candidate")
        elif rehab_ltv <= 0.70:
            score += 22
            strengths.append(f"Deal spread at {rehab_ltv:.0%} of ARV — meets most hard money criteria")
        elif rehab_ltv <= 0.75:
            score += 12
            concerns.append(f"Deal spread at {rehab_ltv:.0%} of ARV — at the edge of most lender maximums")
        else:
            concerns.append(f"Deal spread at {rehab_ltv:.0%} of ARV — above most lender maximums of 70–75%")
    else:
        # Conventional / private — use income-based qualifying
        if borrower.annual_income > 0 and property_.purchase_price > 0:
            # Simple DTI proxy
            piti = _monthly_payment(borrower.desired_loan_amount, 7.5, 360)
            dti = (piti * 12) / borrower.annual_income if borrower.annual_income > 0 else 1
            if dti <= 0.36:
                score += 25
                strengths.append("Strong income-to-payment ratio")
            elif dti <= 0.43:
                score += 15
            else:
                concerns.append("High debt-to-income ratio may limit conventional options")

    # ── LTV / Down payment (20 pts) ──────────────────────────────────────────
    if ltv is not None:
        if ltv <= 0.65:
            score += 20
            strengths.append(f"Strong equity position at {ltv:.0%} LTV")
        elif ltv <= 0.75:
            score += 15
        elif ltv <= 0.80:
            score += 8
        else:
            concerns.append(f"High LTV of {ltv:.0%} — may require additional assets or PMI")
    elif borrower.down_payment_pct >= 30:
        score += 20
        strengths.append(f"Substantial down payment ({borrower.down_payment_pct:.0f}%)")
    elif borrower.down_payment_pct >= 20:
        score += 14
    elif borrower.down_payment_pct >= 15:
        score += 6
        concerns.append(f"Down payment of {borrower.down_payment_pct:.0f}% is below the 20% preferred for investment loans")
    else:
        concerns.append("Down payment under 15% — very limited options for investment properties")

    # ── Liquidity (10 pts) ────────────────────────────────────────────────────
    loan_amt = borrower.desired_loan_amount or property_.purchase_price * 0.75
    reserves_months = (borrower.liquidity / (_monthly_payment(loan_amt, 8.0, 360) or 1))
    if reserves_months >= 12:
        score += 10
        strengths.append(f"Strong reserves ({reserves_months:.0f} months of PITIA)")
    elif reserves_months >= 6:
        score += 7
    elif reserves_months >= 3:
        score += 3
        concerns.append(f"Tight post-close reserves ({reserves_months:.0f} months) — lenders typically want 3–6 months minimum")
    else:
        concerns.append("Minimal post-close reserves may be a deal-breaker for some lenders")

    # ── Experience (10 pts) ───────────────────────────────────────────────────
    props = borrower.properties_owned
    if props >= 5:
        score += 10
        strengths.append(f"Experienced investor ({props} properties owned) — qualifies for best investor terms")
    elif props >= 2:
        score += 7
        strengths.append(f"Prior investment experience ({props} properties)")
    elif props == 1:
        score += 4
    else:
        concerns.append("First investment property — some lenders require 1+ prior investment property")

    return round(min(score, 100), 1), strengths, concerns


def next_steps_for_product(product: str) -> list[str]:
    """Return the standard next-steps list for a given loan product."""
    base = [
        "Complete the full loan application",
        "Upload required documents (government ID, bank statements x3 months)",
    ]
    product_specific = {
        "dscr": [
            "Provide lease agreement or rental market analysis",
            "Order property appraisal (lender will coordinate)",
            "Review DSCR calculation with your loan officer",
        ],
        "fix_flip": [
            "Provide detailed rehab scope of work + contractor bids",
            "Submit ARV appraisal or BPO",
            "Confirm draw schedule with lender",
        ],
        "brrrr": [
            "Complete rehab and obtain CO (certificate of occupancy)",
            "Place tenant and collect 1–2 months rent",
            "Apply for DSCR refi after 6-month seasoning",
        ],
        "conventional": [
            "Provide 2 years tax returns + W-2s",
            "Pull tri-merge credit report",
            "Get pre-approval letter before making offers",
        ],
        "hard_money": [
            "Submit purchase contract",
            "Provide rehab scope and contractor bids",
            "Lender will order BPO/appraisal — typically 3–5 business days",
        ],
        "private": [
            "Prepare executive summary of the deal",
            "Provide proof of funds / entity docs",
            "Negotiate terms directly with private lender",
        ],
    }
    return base + product_specific.get(product, [])
