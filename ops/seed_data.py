"""
ops/seed_data.py — Insert sample data for local smoke testing.

Creates:
  - A sample prequal for Marc's Detroit property
  - A sample loan application (state: APP_DOCS_PENDING)
  - A sample pre-underwriting report for that application

Run: python ops/seed_data.py
"""

import os
import sys
import json
import uuid
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from shared.db import init_db, get_conn, insert, fetchone


def _now():
    return datetime.now(timezone.utc).isoformat()


def _already_seeded(conn) -> bool:
    row = fetchone(conn, "SELECT id FROM loan_prequals WHERE id = ?", ("pq_seed_marc_001",))
    return row is not None


def seed():
    print("[seed] Initializing database...")
    init_db()

    with get_conn() as conn:
        if _already_seeded(conn):
            print("[seed] Already seeded — skipping.")
            return

    now = _now()
    closing_date = (date.today() + timedelta(days=30)).isoformat()

    # ── Prequal ────────────────────────────────────────────────────────────────
    prequal_id = "pq_seed_marc_001"
    borrower_data = {
        "user_id": "usr_marc",
        "name": "Marc Munoz",
        "email": "marc@munoz.ltd",
        "phone": "+13135550100",
        "annual_income": 120_000,
        "credit_score": 740,
        "properties_owned": 3,
        "liquidity": 85_000,
        "loan_purpose": "purchase",
        "desired_loan_amount": 71_250,
        "down_payment": 23_750,
        "down_payment_pct": 25,
    }
    property_data = {
        "address": "4521 Oak Ln, Detroit MI 48224",
        "property_type": "single_family",
        "purchase_price": 95_000,
        "estimated_value": 110_000,
        "monthly_rent": 1_200,
        "annual_taxes": 2_400,
        "annual_insurance": 1_200,
        "hoa_monthly": 0,
        "condition": "good",
        "rehab_budget": 0,
        "arv": 0,
    }

    with get_conn() as conn:
        insert(conn, "loan_prequals", {
            "id": prequal_id,
            "user_id": "usr_marc",
            "borrower_data": json.dumps(borrower_data),
            "property_data": json.dumps(property_data),
            "score": 78.5,
            "suggested_product": "dscr",
            "dscr": 1.18,
            "ltv": 0.648,
            "monthly_payment_estimate": 522.48,
            "strengths": json.dumps([
                "Strong credit score (740)",
                "DSCR of 1.18 meets requirements",
                "25% down payment meets lender standards",
                "Prior investment experience (3 properties)",
            ]),
            "concerns": json.dumps([
                "Reserves of 11 months are adequate but not exceptional",
            ]),
            "next_steps": json.dumps([
                "Complete the full loan application",
                "Upload required documents (government ID, bank statements x3 months)",
                "Provide lease agreement or rental market analysis",
                "Order property appraisal (lender will coordinate)",
                "Review DSCR calculation with your loan officer",
            ]),
            "status": "scored",
            "notes": "Seed data — Detroit 4521 Oak Ln",
            "created_at": now,
            "updated_at": now,
        })
        print(f"[seed] Created prequal: {prequal_id}")

    # ── Loan Application ───────────────────────────────────────────────────────
    app_id = "app_seed_marc_001"
    audit_log = [
        {
            "event_type": "application_created",
            "actor": "usr_marc",
            "payload": {"prequal_id": prequal_id, "product": "dscr"},
            "ts": now,
        },
        {
            "event_type": "state_transition",
            "from_state": "APP_STARTED",
            "to_state": "APP_DOCS_PENDING",
            "actor": "system",
            "action": "Send document checklist to borrower",
            "payload": {},
            "ts": now,
        },
    ]

    with get_conn() as conn:
        insert(conn, "loan_applications", {
            "id": app_id,
            "prequal_id": prequal_id,
            "user_id": "usr_marc",
            "status": "APP_DOCS_PENDING",
            "current_state": "APP_DOCS_PENDING",
            "lender_partner": "kiavi",
            "lender_ref_id": "",
            "docs_required": json.dumps(["government_id", "bank_statement_3mo", "purchase_contract", "lease_agreement", "entity_docs"]),
            "docs_received": json.dumps(["government_id"]),
            "underwriter_notes": "",
            "approved_amount": None,
            "approved_rate": None,
            "approved_term": None,
            "conditions": "[]",
            "audit_log": json.dumps(audit_log),
            "created_at": now,
            "updated_at": now,
        })
        print(f"[seed] Created loan application: {app_id}")

        insert(conn, "loan_documents", {
            "application_id": app_id,
            "doc_type": "government_id",
            "s3_url": "s3://tranchi-docs-dev/usr_marc/drivers_license.jpg",
            "verified": 0,
            "uploaded_by": "usr_marc",
            "notes": "Seed document",
            "uploaded_at": now,
        })

    # ── Sample Pre-Underwriting Report ────────────────────────────────────────
    conditions_sample = [
        {
            "condition_type": "doc_request",
            "severity": "prior_to_submission",
            "description": "Tri-merge credit report (lender will order — do not submit broker-pulled credit)",
            "lender_specific": "lima_one_dscr",
            "required": True,
        },
        {
            "condition_type": "doc_request",
            "severity": "prior_to_submission",
            "description": "Executed purchase contract",
            "lender_specific": "all",
            "required": True,
        },
        {
            "condition_type": "doc_request",
            "severity": "prior_to_close",
            "description": "DSCR appraisal with market rent schedule (Form 1007)",
            "lender_specific": "lima_one_dscr",
            "required": True,
        },
        {
            "condition_type": "doc_request",
            "severity": "prior_to_close",
            "description": "Bank statements — 2 most recent months, all pages (6 months PITI reserves)",
            "lender_specific": "lima_one_dscr",
            "required": True,
        },
    ]
    lender_fit_sample = [
        {
            "lender_id": "lima_one_dscr",
            "lender": "Lima One Capital",
            "product": "DSCR Long-Term Rental",
            "product_type": "dscr",
            "fit_score": 88,
            "qualifies": True,
            "qualify_reasons": ["FICO 740 meets min 660", "LTV 64.8% within 80% max", "DSCR 1.18 meets min 1.10"],
            "decline_reasons": [],
            "hot_buttons": ["DSCR calculated on gross rent only", "Short-term rental proration applies"],
            "rate_range_pct": [7.25, 9.5],
        },
        {
            "lender_id": "kiavi_dscr",
            "lender": "Kiavi",
            "product": "Rental Loan (DSCR)",
            "product_type": "dscr",
            "fit_score": 85,
            "qualifies": True,
            "qualify_reasons": ["FICO 740 meets min 640", "LTV 64.8% within 80% max", "DSCR 1.18 meets min 1.00"],
            "decline_reasons": [],
            "hot_buttons": ["No condos for DSCR", "Max 10 financed properties"],
            "rate_range_pct": [7.0, 9.25],
        },
    ]

    with get_conn() as conn:
        conn.execute("DELETE FROM pre_underwriting_reports WHERE application_id = ?", (app_id,))
        conn.commit()
        insert(conn, "pre_underwriting_reports", {
            "application_id": app_id,
            "status": "clean",
            "summary": "Clean file for Lima One DSCR — 2 PTSU conditions",
            "overall_status": "clean",
            "lender_fit": json.dumps(lender_fit_sample),
            "conditions": json.dumps(conditions_sample),
            "red_flags": json.dumps([]),
            "computed_metrics": json.dumps({
                "fico": 740,
                "loan_amount": 71250.0,
                "ltv": 0.648,
                "ltc": 0.0,
                "monthly_pni": 522.48,
                "monthly_piti": 722.48,
                "monthly_cashflow": 477.52,
                "dscr": 1.18,
                "dscr_coverage_gap": 0.08,
                "monthly_rent": 1200,
                "arv": 0,
                "purchase_price": 95000,
                "rehab_budget": 0,
                "down_pct": 25.0,
                "product_type": "dscr",
            }),
            "suggested_lender": "Lima One Capital",
            "credit_memo": (
                "## Borrower\nMarc Munoz. Estimated FICO: 740. Reported annual income: $120,000. "
                "Liquid assets: $85,000. Properties owned: 3. Loan purpose: purchase.\n\n"
                "## Subject Property\n4521 Oak Ln, Detroit MI 48224. SFR. Purchase price: $95,000. "
                "Monthly rent (projected): $1,200/mo.\n\n"
                "## Transaction\nPurchase. Loan amount: $71,250. 25% down. LTV 64.8%.\n\n"
                "## Cash Flow\nDSCR 1.18 (proforma). Monthly PITI: $722. Net cash flow: +$478/mo.\n\n"
                "## Risk Factors\nProforma rent — appraiser must confirm. DSCR has 8bps coverage "
                "above Lima One's 1.10 floor. Minor sensitivity to appraisal.\n\n"
                "## Recommendation\nClean file. Submit to Lima One DSCR as primary. "
                "Kiavi as backup (1.00 DSCR min). Clear PTSU conditions first."
            ),
            "generated_at": now,
        })
        print(f"[seed] Created pre-underwriting report for: {app_id}")

    print("\n[seed] Done. Sample IDs:")
    print(f"  Prequal:     {prequal_id}")
    print(f"  Application: {app_id}")
    print(f"\nSmoke test commands:")
    print(f"  curl http://localhost:5010/health")
    print(f"  curl -H 'Authorization: Bearer dev-secret-change-me' http://localhost:5010/api/loan/prequal/{prequal_id}")
    print(f"  curl -H 'Authorization: Bearer dev-secret-change-me' http://localhost:5010/api/processor/pre-underwrite/{app_id}")
    print(f"  curl -H 'Authorization: Bearer dev-secret-change-me' http://localhost:5010/api/processor/guidelines")


if __name__ == "__main__":
    seed()
