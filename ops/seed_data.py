"""
ops/seed_data.py — Insert sample data for local smoke testing.

Creates:
  - A sample prequal for Marc's Detroit property
  - A sample loan application (state: APP_DOCS_PENDING)
  - A sample transaction at milestone 3 (inspection scheduled)

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

    # ── Transaction ────────────────────────────────────────────────────────────
    tx_id = "tx_seed_marc_001"
    psa_terms = {
        "purchase_price": 95_000,
        "earnest_money": 2_500,
        "closing_date": closing_date,
        "inspection_period_days": 10,
        "financing_contingency_days": 21,
        "title_contingency_days": 14,
        "seller_concessions": 0,
        "buyer_name": "Marc Munoz",
        "buyer_email": "marc@munoz.ltd",
        "buyer_phone": "+13135550100",
        "seller_name": "John Smith",
        "seller_email": "jsmith@example.com",
        "seller_phone": "+13135559876",
        "buyer_agent_name": "Sarah Jones",
        "listing_agent_name": "Bob Williams",
        "property_address": "4521 Oak Ln, Detroit MI 48224",
        "notes": "Seed transaction for local dev testing",
    }

    with get_conn() as conn:
        insert(conn, "transactions", {
            "id": tx_id,
            "user_id": "usr_marc",
            "psa_terms": json.dumps(psa_terms),
            "purchase_price": 95_000,
            "closing_date": closing_date,
            "status": "open",
            "current_milestone": "inspection_scheduled",
            "property_address": "4521 Oak Ln, Detroit MI 48224",
            "buyer_name": "Marc Munoz",
            "seller_name": "John Smith",
            "notes": "Seed transaction",
            "created_at": now,
            "updated_at": now,
        })
        print(f"[seed] Created transaction: {tx_id}")

        # Insert parties
        parties = [
            ("buyer", "Marc Munoz", "marc@munoz.ltd", "+13135550100", ""),
            ("seller", "John Smith", "jsmith@example.com", "+13135559876", ""),
            ("buyer_agent", "Sarah Jones", "sjones@realty.com", "+13135551234", "Tranchi Realty"),
            ("listing_agent", "Bob Williams", "bwilliams@realty.com", "+13135555678", "Detroit Properties Inc"),
            ("inspector", "Detroit Home Inspectors LLC", "schedule@dhiinspect.com", "+13135559999", "Detroit Home Inspectors LLC"),
        ]
        for party_type, name, email, phone, company in parties:
            conn.execute(
                """INSERT INTO tx_parties (transaction_id, party_type, name, email, phone, company, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (tx_id, party_type, name, email, phone, company, now)
            )
        conn.commit()

        # Insert milestones (first 3 completed, rest pending)
        from tx_coordinator.timeline import generate_timeline
        from shared.schemas import PSATerms
        psa = PSATerms(**psa_terms)
        milestones = generate_timeline(psa)

        for m in milestones:
            status = "pending"
            completed_at = None
            # Mark first 3 as completed for seed scenario: PSA executed, earnest money, title ordered
            if m["name"] in ("psa_executed", "earnest_money_deposited", "title_ordered"):
                status = "completed"
                completed_at = now
            conn.execute(
                """INSERT INTO tx_milestones
                   (transaction_id, milestone_name, milestone_label, sequence_order,
                    target_date, status, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (tx_id, m["name"], m["label"], m["sequence"],
                 m["target_date"], status, completed_at)
            )
        conn.commit()

        # Insert contingency deadlines
        for ctype, days in [("inspection", 10), ("financing", 21), ("title", 14)]:
            deadline = (date.today() + timedelta(days=days)).isoformat()
            conn.execute(
                """INSERT INTO tx_deadlines (transaction_id, contingency_type, deadline_date, status)
                   VALUES (?, ?, ?, 'active')""",
                (tx_id, ctype, deadline)
            )
        conn.commit()

        # Insert sample PSA document
        insert(conn, "tx_documents", {
            "transaction_id": tx_id,
            "doc_type": "psa",
            "s3_url": "s3://tranchi-docs-dev/tx_seed/psa_signed.pdf",
            "party_uploaded": "buyer_agent",
            "status": "received",
            "notes": "Seed PSA document",
            "uploaded_at": now,
        })

        # Log a communication
        conn.execute(
            """INSERT INTO tx_communications
               (transaction_id, direction, channel, summary, full_text, occurred_at, logged_at)
               VALUES (?, 'out', 'email', 'Sent inspection scheduling request to inspector.', '', ?, ?)""",
            (tx_id, now, now)
        )
        conn.commit()

    print("\n[seed] Done. Sample IDs:")
    print(f"  Prequal:     {prequal_id}")
    print(f"  Application: {app_id}")
    print(f"  Transaction: {tx_id}")
    print(f"\nSmoke test commands:")
    print(f"  curl http://localhost:5010/health")
    print(f"  curl -H 'Authorization: Bearer dev-secret-change-me' http://localhost:5010/api/loan/prequal/{prequal_id}")
    print(f"  curl -H 'Authorization: Bearer dev-secret-change-me' http://localhost:5010/api/tx/{tx_id}")


if __name__ == "__main__":
    seed()
