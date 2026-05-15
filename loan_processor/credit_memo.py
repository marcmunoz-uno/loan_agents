"""
loan_processor/credit_memo.py — Draft credit memos for underwriters.

Uses the LLM (Claude Sonnet via shared/llm.py) with the borrower data,
property data, computed metrics, and the lender's full guidelines as context.

The output is a ~300-word professional narrative in underwriting language.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from shared.llm import chat
from loan_processor.guideline_engine import get_engine

logger = logging.getLogger(__name__)

_CREDIT_MEMO_SYSTEM = """You are a senior mortgage loan processor drafting a credit memo for an underwriter.

Write a professional, ~300-word narrative covering: Borrower profile, Subject Property, Transaction structure, Cash Flow analysis, Risk factors, and a clear Recommendation (approve at X terms / counter at Y terms / decline with reason).

Use precise underwriting language. Be terse and factual. No preamble. No disclaimers. Structure with the following headers exactly:
## Borrower
## Subject Property
## Transaction
## Cash Flow
## Risk Factors
## Recommendation"""


def draft_credit_memo(
    borrower_data: dict[str, Any],
    property_data: dict[str, Any],
    computed_metrics: dict[str, Any],
    suggested_lender: str,
    overall_status: str,
    conditions_summary: list[str],
    lender_id: Optional[str] = None,
) -> str:
    """
    Draft a credit memo narrative.

    Returns the memo as a plain string (markdown formatted).
    Falls back to a structured template if LLM is unavailable.
    """
    # Build structured context
    context = {
        "borrower": _safe_borrower(borrower_data),
        "property": _safe_property(property_data),
        "metrics": computed_metrics,
        "suggested_lender": suggested_lender,
        "overall_status": overall_status,
        "conditions_count": len(conditions_summary),
        "top_conditions": conditions_summary[:5],
    }

    # Load lender guidelines as context if available
    guidelines_context = ""
    if lender_id:
        engine = get_engine()
        doc = engine.get_guideline_doc(lender_id)
        if doc:
            guidelines_context = f"\n\n---\nLENDER GUIDELINES ({lender_id}):\n{doc[:3000]}"

    prompt = f"""Draft a credit memo for the following loan file.

FILE DATA:
{json.dumps(context, indent=2, default=str)}
{guidelines_context}

Write the credit memo now."""

    try:
        memo = chat(
            messages=[{"role": "user", "content": prompt}],
            system=_CREDIT_MEMO_SYSTEM,
            model_tier="standard",
            max_tokens=600,
            temperature=0.2,
        )
        return memo.strip()
    except Exception as exc:
        logger.error("[credit_memo] LLM call failed: %s — returning template memo", exc)
        return _template_memo(borrower_data, property_data, computed_metrics,
                              suggested_lender, overall_status)


def _safe_borrower(b: dict) -> dict:
    return {
        "name": b.get("name", ""),
        "credit_score_estimate": b.get("credit_score", "unknown"),
        "annual_income": b.get("annual_income", 0),
        "liquidity": b.get("liquidity", 0),
        "properties_owned": b.get("properties_owned", 0),
        "loan_purpose": b.get("loan_purpose", "purchase"),
    }


def _safe_property(p: dict) -> dict:
    return {
        "address": p.get("address", ""),
        "property_type": p.get("property_type", ""),
        "purchase_price": p.get("purchase_price", 0),
        "estimated_value": p.get("estimated_value", 0),
        "monthly_rent": p.get("monthly_rent", 0),
        "annual_taxes": p.get("annual_taxes", 0),
        "annual_insurance": p.get("annual_insurance", 0),
        "condition": p.get("condition", ""),
        "rehab_budget": p.get("rehab_budget", 0),
        "arv": p.get("arv", 0),
    }


def _template_memo(
    borrower_data: dict,
    property_data: dict,
    computed_metrics: dict,
    suggested_lender: str,
    overall_status: str,
) -> str:
    """Fallback template memo when LLM is unavailable."""
    b = _safe_borrower(borrower_data)
    p = _safe_property(property_data)
    m = computed_metrics

    dscr = m.get("dscr", "N/A")
    ltv = m.get("ltv", "N/A")
    ltv_str = f"{ltv:.0%}" if isinstance(ltv, float) else str(ltv)
    dscr_str = f"{dscr:.2f}" if isinstance(dscr, float) else str(dscr)

    return f"""## Borrower
{b['name']}. Estimated FICO: {b['credit_score_estimate']}. Reported annual income: ${b['annual_income']:,.0f}. Liquid assets: ${b['liquidity']:,.0f}. Properties owned: {b['properties_owned']}. Loan purpose: {b['loan_purpose']}.

## Subject Property
{p['address']}. Property type: {p['property_type']}. Purchase price: ${p['purchase_price']:,.0f}. Estimated value: ${p['estimated_value']:,.0f}. Monthly rent (projected): ${p['monthly_rent']:,.0f}/mo. Annual taxes: ${p['annual_taxes']:,.0f}. Annual insurance: ${p['annual_insurance']:,.0f}.

## Transaction
{b['loan_purpose'].title()} of investment property. Loan amount: ${m.get('loan_amount', 0):,.0f}. Down payment: {ltv_str} LTV.

## Cash Flow
DSCR: {dscr_str} (calculated on {'actual' if p['monthly_rent'] else 'proforma'} rent). Monthly P&I estimate: ${m.get('monthly_pni', 0):,.0f}. Monthly PITI: ${m.get('monthly_piti', 0):,.0f}. Monthly net cash flow: ${m.get('monthly_cashflow', 0):,.0f}.

## Risk Factors
Standard DSCR investment property. No identified material risk factors beyond those noted in conditions list. Recommend pulling tri-merge credit before final lender selection.

## Recommendation
Based on the information provided, file appears {overall_status}. Suggested lender: {suggested_lender}. Full pre-underwriting report attached. MLO to review conditions list and confirm borrower can clear PTSUs before submission.
"""
