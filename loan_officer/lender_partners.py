"""
loan_officer/lender_partners.py — Configurable lender partner registry.

Each entry defines:
  - products: which loan types the lender handles
  - criteria: rough qualification thresholds
  - api_endpoint: placeholder for their application/submission API
  - webhook_url: where they push status updates (approval, decline, conditions)
  - contact: human contact at the lender for escalations

In production, this dict will be loaded from a database table so lender partners
can be added/updated without a code deploy.
"""

from __future__ import annotations
from typing import Any, Optional

# ── Lender Partner Registry ───────────────────────────────────────────────────

LENDER_PARTNERS: dict[str, dict[str, Any]] = {
    "lima_one": {
        "name": "Lima One Capital",
        "slug": "lima_one",
        "website": "https://limaone.com",
        "products": ["dscr", "fix_flip", "multifamily"],
        "states": ["all"],  # nationwide
        "criteria": {
            "min_credit_score": 660,
            "min_loan_amount": 75_000,
            "max_loan_amount": 5_000_000,
            "min_dscr": 1.0,
            "max_ltv_dscr": 0.80,
            "max_ltv_fix_flip_arv": 0.75,
            "min_down_payment_pct": 20,
        },
        "rates": {
            "dscr": {"low": 7.25, "high": 9.5},
            "fix_flip": {"low": 9.5, "high": 12.0},
        },
        "typical_close_days": {
            "dscr": 21,
            "fix_flip": 10,
        },
        # TODO: integrate with Lima One Capital API
        # Docs: https://limaone.com/api (placeholder)
        "api_endpoint": "https://api.limaone.com/v1/applications",
        "webhook_url": "https://dealflow.tranchi.ai/api/loan/webhook/lender-update",
        "api_key_env": "LIMA_ONE_API_KEY",
        "contact": {
            "name": "Wholesale Desk",
            "email": "wholesale@limaone.com",
            "phone": "1-800-404-5462",
        },
    },

    "kiavi": {
        "name": "Kiavi",
        "slug": "kiavi",
        "website": "https://kiavi.com",
        "products": ["dscr", "fix_flip", "brrrr"],
        "states": ["all"],
        "criteria": {
            "min_credit_score": 640,
            "min_loan_amount": 100_000,
            "max_loan_amount": 3_000_000,
            "min_dscr": 1.0,
            "max_ltv_dscr": 0.80,
            "max_ltv_fix_flip_arv": 0.75,
            "min_down_payment_pct": 20,
        },
        "rates": {
            "dscr": {"low": 7.0, "high": 9.25},
            "fix_flip": {"low": 9.0, "high": 11.5},
        },
        "typical_close_days": {
            "dscr": 25,
            "fix_flip": 10,
        },
        # TODO: integrate with Kiavi API
        # Docs: https://kiavi.com/api/docs (placeholder)
        "api_endpoint": "https://api.kiavi.com/v1/loan-applications",
        "webhook_url": "https://dealflow.tranchi.ai/api/loan/webhook/lender-update",
        "api_key_env": "KIAVI_API_KEY",
        "contact": {
            "name": "Investor Relations",
            "email": "loans@kiavi.com",
            "phone": "1-888-895-9814",
        },
    },

    "new_silver": {
        "name": "New Silver",
        "slug": "new_silver",
        "website": "https://newsilver.com",
        "products": ["fix_flip", "new_construction"],
        "states": ["all"],
        "criteria": {
            "min_credit_score": 620,
            "min_loan_amount": 100_000,
            "max_loan_amount": 5_000_000,
            "max_ltv_fix_flip_arv": 0.75,
            "min_down_payment_pct": 10,
        },
        "rates": {
            "fix_flip": {"low": 9.5, "high": 12.5},
        },
        "typical_close_days": {
            "fix_flip": 7,
        },
        # TODO: integrate with New Silver API (they have an instant online pre-qual)
        "api_endpoint": "https://api.newsilver.com/v1/prequalify",
        "webhook_url": "https://dealflow.tranchi.ai/api/loan/webhook/lender-update",
        "api_key_env": "NEW_SILVER_API_KEY",
        "contact": {
            "name": "Broker Desk",
            "email": "brokers@newsilver.com",
            "phone": "1-646-363-6434",
        },
    },

    "lending_one": {
        "name": "LendingOne",
        "slug": "lending_one",
        "website": "https://lendingone.com",
        "products": ["dscr", "multifamily"],
        "states": ["all"],
        "criteria": {
            "min_credit_score": 620,
            "min_loan_amount": 75_000,
            "max_loan_amount": 10_000_000,
            "min_dscr": 0.95,
            "max_ltv_dscr": 0.80,
            "min_down_payment_pct": 20,
        },
        "rates": {
            "dscr": {"low": 7.25, "high": 9.75},
        },
        "typical_close_days": {
            "dscr": 21,
        },
        # TODO: integrate with LendingOne broker portal API
        "api_endpoint": "https://broker.lendingone.com/api/submit",
        "webhook_url": "https://dealflow.tranchi.ai/api/loan/webhook/lender-update",
        "api_key_env": "LENDING_ONE_API_KEY",
        "contact": {
            "name": "Broker Support",
            "email": "brokers@lendingone.com",
            "phone": "1-866-708-2896",
        },
    },

    "roc_capital": {
        "name": "Roc Capital",
        "slug": "roc_capital",
        "website": "https://roccapital.com",
        "products": ["dscr", "fix_flip", "brrrr", "multifamily"],
        "states": ["all"],
        "criteria": {
            "min_credit_score": 620,
            "min_loan_amount": 100_000,
            "max_loan_amount": 5_000_000,
            "min_dscr": 1.0,
            "max_ltv_dscr": 0.80,
            "max_ltv_fix_flip_arv": 0.70,
            "min_down_payment_pct": 20,
        },
        "rates": {
            "dscr": {"low": 7.5, "high": 9.5},
            "fix_flip": {"low": 10.0, "high": 13.0},
        },
        "typical_close_days": {
            "dscr": 21,
            "fix_flip": 14,
        },
        # TODO: integrate with Roc Capital broker API
        "api_endpoint": "https://api.roccapital.com/broker/applications",
        "webhook_url": "https://dealflow.tranchi.ai/api/loan/webhook/lender-update",
        "api_key_env": "ROC_CAPITAL_API_KEY",
        "contact": {
            "name": "Broker Relations",
            "email": "brokers@roccapital.com",
            "phone": "1-888-ROC-CAPITAL",
        },
    },

    "anchor_loans": {
        "name": "Anchor Loans",
        "slug": "anchor_loans",
        "website": "https://anchorloans.com",
        "products": ["fix_flip", "new_construction"],
        "states": ["CA", "WA", "OR", "AZ", "NV", "CO", "TX", "FL", "GA", "NC", "SC"],
        "criteria": {
            "min_credit_score": 650,
            "min_loan_amount": 100_000,
            "max_loan_amount": 20_000_000,
            "max_ltv_fix_flip_arv": 0.70,
            "min_down_payment_pct": 10,
        },
        "rates": {
            "fix_flip": {"low": 9.5, "high": 12.0},
        },
        "typical_close_days": {
            "fix_flip": 7,
        },
        # TODO: integrate with Anchor Loans broker platform
        "api_endpoint": "https://broker.anchorloans.com/api/v2/applications",
        "webhook_url": "https://dealflow.tranchi.ai/api/loan/webhook/lender-update",
        "api_key_env": "ANCHOR_LOANS_API_KEY",
        "contact": {
            "name": "Broker Hotline",
            "email": "brokers@anchorloans.com",
            "phone": "1-800-ANCHOR1",
        },
    },
}


def get_lender(slug: str) -> Optional[dict[str, Any]]:
    """Fetch a single lender by slug."""
    return LENDER_PARTNERS.get(slug)


def lenders_for_product(product: str) -> list[dict[str, Any]]:
    """Return all lenders that handle a given product type."""
    return [
        lender for lender in LENDER_PARTNERS.values()
        if product in lender["products"]
    ]


def lenders_for_state(state: str) -> list[dict[str, Any]]:
    """Return all lenders that operate in a given state (2-letter code)."""
    return [
        lender for lender in LENDER_PARTNERS.values()
        if "all" in lender["states"] or state.upper() in lender["states"]
    ]
