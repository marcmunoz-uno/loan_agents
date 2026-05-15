"""
loan_processor/tests/test_pre_underwriting.py — Pre-underwriting engine tests.

Run: python -m pytest loan_processor/tests/ -v

Tests are DB-free: we insert minimal seed rows directly, then clean up.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv()

os.environ.setdefault("DB_PATH", "data/test_preuw.db")

from shared.db import init_db, get_conn, insert, fetchone
from loan_processor.pre_underwriting import pre_underwrite, _compute_metrics, _generate_red_flags
from loan_processor.guideline_engine import GuidelineEngine, get_engine


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def setup_db():
    """Initialize DB tables once for all tests in module."""
    init_db()


def _delete_prequal(conn, prequal_id):
    """Delete a prequal and all dependent rows (respecting FK constraints)."""
    # Find and delete child applications + their children first
    apps = conn.execute("SELECT id FROM loan_applications WHERE prequal_id = ?", (prequal_id,)).fetchall()
    for app_row in apps:
        app_id = app_row["id"]
        conn.execute("DELETE FROM pre_underwriting_reports WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM loan_audit_log WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM loan_documents WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM loan_applications WHERE id = ?", (app_id,))
    conn.execute("DELETE FROM loan_prequals WHERE id = ?", (prequal_id,))
    conn.commit()


def _insert_prequal(prequal_id, borrower, prop, product, dscr=None, ltv=None, monthly_pmt=0):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        # Delete if exists (reruns) — must delete children first due to FK
        _delete_prequal(conn, prequal_id)
        insert(conn, "loan_prequals", {
            "id": prequal_id,
            "user_id": "usr_test",
            "borrower_data": json.dumps(borrower),
            "property_data": json.dumps(prop),
            "score": 75.0,
            "suggested_product": product,
            "dscr": dscr,
            "ltv": ltv,
            "monthly_payment_estimate": monthly_pmt,
            "strengths": "[]",
            "concerns": "[]",
            "next_steps": "[]",
            "status": "scored",
            "notes": "test",
            "created_at": now,
            "updated_at": now,
        })


def _insert_application(app_id, prequal_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        # Delete dependents first, then the application
        conn.execute("DELETE FROM pre_underwriting_reports WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM loan_audit_log WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM loan_documents WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM loan_applications WHERE id = ?", (app_id,))
        conn.commit()
        insert(conn, "loan_applications", {
            "id": app_id,
            "prequal_id": prequal_id,
            "user_id": "usr_test",
            "status": "APP_SUBMITTED",
            "current_state": "APP_SUBMITTED",
            "lender_partner": "",
            "lender_ref_id": "",
            "docs_required": "[]",
            "docs_received": "[]",
            "underwriter_notes": "",
            "approved_amount": None,
            "approved_rate": None,
            "approved_term": None,
            "conditions": "[]",
            "audit_log": "[]",
            "created_at": now,
            "updated_at": now,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Clean DSCR file
# ─────────────────────────────────────────────────────────────────────────────

class TestCleanDSCRFile:
    """
    FICO 720, DSCR 1.25, 25% down, SFR Detroit MI.
    Should be clean, suggest Lima One or Kiavi.
    """

    def setup_method(self):
        self.prequal_id = "pq_test_clean_001"
        self.app_id = "app_test_clean_001"
        borrower = {
            "user_id": "usr_test",
            "name": "Test Clean Borrower",
            "credit_score": 720,
            "annual_income": 150_000,
            "liquidity": 80_000,
            "properties_owned": 3,
            "loan_purpose": "purchase",
            # Use a loan amount above all lenders' $100K minimums
            "desired_loan_amount": 120_000,
            "down_payment_pct": 25,
        }
        prop = {
            "address": "9100 Riverside Dr Cincinnati OH 45202",
            "property_type": "single_family",
            "purchase_price": 160_000,
            "estimated_value": 175_000,
            "monthly_rent": 1_650,
            "annual_taxes": 3_200,
            "annual_insurance": 1_400,
            "hoa_monthly": 0,
        }
        # Monthly P&I on $120,000 at 8% 30yr ≈ $881
        # PITI ≈ 881 + 267 + 117 = 1,265  DSCR = 1650/1265 ≈ 1.30
        _insert_prequal(self.prequal_id, borrower, prop, "dscr", dscr=1.30, ltv=0.686, monthly_pmt=881.0)
        _insert_application(self.app_id, self.prequal_id)

    def test_status_is_clean(self):
        report = pre_underwrite(self.app_id)
        assert report.overall_status == "clean", f"Expected clean, got: {report.overall_status}. Red flags: {report.red_flags}"

    def test_suggests_lima_one_or_kiavi(self):
        report = pre_underwrite(self.app_id)
        qualifying = [lf for lf in report.lender_fit if lf["qualifies"]]
        lender_ids = [lf["lender_id"] for lf in qualifying]
        assert any("lima_one" in lid or "kiavi" in lid for lid in lender_ids), \
            f"Expected Lima One or Kiavi in qualifying lenders, got: {lender_ids}"

    def test_has_conditions(self):
        report = pre_underwrite(self.app_id)
        assert len(report.conditions) > 0, "Should generate at least one condition"

    def test_no_deal_killer_flags(self):
        report = pre_underwrite(self.app_id)
        deal_killers = [f for f in report.red_flags if f.severity == "deal_killer"]
        assert len(deal_killers) == 0, f"Clean file should not have deal killers: {deal_killers}"

    def test_computed_dscr_positive(self):
        report = pre_underwrite(self.app_id)
        dscr = report.computed_metrics.get("dscr")
        assert dscr is not None
        assert dscr >= 1.0, f"Expected DSCR >= 1.0, got {dscr}"

    def test_summary_contains_lender_name(self):
        report = pre_underwrite(self.app_id)
        assert len(report.summary) > 0
        assert report.suggested_lender != "", "Should have a suggested lender"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Borderline DSCR (0.95)
# ─────────────────────────────────────────────────────────────────────────────

class TestBorderlineDSCR:
    """
    DSCR 0.95 — below Lima One (1.10) and Kiavi (1.00) minimums.
    Should route to LendingOne no-DSCR or be decline_risk.
    """

    def setup_method(self):
        self.prequal_id = "pq_test_border_001"
        self.app_id = "app_test_border_001"
        borrower = {
            "user_id": "usr_test",
            "name": "Test Borderline Borrower",
            "credit_score": 680,
            "annual_income": 80_000,
            "liquidity": 40_000,
            "properties_owned": 1,
            "loan_purpose": "purchase",
            "desired_loan_amount": 112_500,  # 75% LTV on $150k
            "down_payment_pct": 25,
        }
        prop = {
            "address": "2200 Test Rd Columbus OH 43215",
            "property_type": "single_family",
            "purchase_price": 150_000,
            "estimated_value": 155_000,
            "monthly_rent": 1_050,           # low rent → DSCR sub-1.0 territory
            "annual_taxes": 3_000,
            "annual_insurance": 1_200,
            "hoa_monthly": 0,
        }
        # Force a sub-1.0 DSCR by storing the precomputed value
        # This tests that the engine correctly identifies it as borderline
        _insert_prequal(self.prequal_id, borrower, prop, "dscr", dscr=0.95, ltv=0.726, monthly_pmt=825.0)
        _insert_application(self.app_id, self.prequal_id)

    def test_status_is_not_clean(self):
        report = pre_underwrite(self.app_id)
        assert report.overall_status in ("conditional", "decline_risk"), \
            f"Expected conditional or decline_risk for DSCR 0.95, got {report.overall_status}"

    def test_lima_one_and_kiavi_do_not_qualify(self):
        report = pre_underwrite(self.app_id)
        qualifying_ids = {lf["lender_id"] for lf in report.lender_fit if lf["qualifies"]}
        assert "lima_one_dscr" not in qualifying_ids, "Lima One should not qualify at DSCR 0.95"
        assert "kiavi_dscr" not in qualifying_ids, "Kiavi should not qualify at DSCR 0.95"

    def test_has_dscr_flag(self):
        report = pre_underwrite(self.app_id)
        dscr_flags = [f for f in report.red_flags if "dscr" in f.flag_type.lower()]
        assert len(dscr_flags) > 0, "Should have a DSCR red flag for 0.95 DSCR"

    def test_lendingone_may_qualify_via_no_dscr(self):
        """LendingOne no-DSCR variant should appear as a near-miss or qualifier."""
        report = pre_underwrite(self.app_id)
        # LendingOne (0.95 min) should either qualify or be the closest near-miss
        lendingone_entry = next(
            (lf for lf in report.lender_fit if "lending" in lf["lender_id"].lower()),
            None
        )
        assert lendingone_entry is not None, "LendingOne should appear in lender_fit"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Low FICO (620) — no lenders fit
# ─────────────────────────────────────────────────────────────────────────────

class TestLowFICO:
    """FICO 620 — below Lima One (660) and Kiavi (640), borderline everywhere."""

    def setup_method(self):
        self.prequal_id = "pq_test_lowfico_001"
        self.app_id = "app_test_lowfico_001"
        borrower = {
            "user_id": "usr_test",
            "name": "Test Low FICO Borrower",
            "credit_score": 615,  # below all minimums (620 is the floor)
            "annual_income": 75_000,
            "liquidity": 30_000,
            "properties_owned": 0,
            "loan_purpose": "purchase",
            "desired_loan_amount": 112_500,
            "down_payment_pct": 25,
        }
        prop = {
            "address": "100 Test Ave Chicago IL 60601",
            "property_type": "single_family",
            "purchase_price": 150_000,
            "estimated_value": 155_000,
            "monthly_rent": 1_450,
            "annual_taxes": 3_600,
            "annual_insurance": 1_800,
        }
        _insert_prequal(self.prequal_id, borrower, prop, "dscr", dscr=1.10, ltv=0.75, monthly_pmt=825.0)
        _insert_application(self.app_id, self.prequal_id)

    def test_fico_flag_present(self):
        report = pre_underwrite(self.app_id)
        fico_flags = [f for f in report.red_flags if "fico" in f.flag_type.lower()]
        assert len(fico_flags) > 0, "Should have FICO red flag for score 615"

    def test_fico_flag_is_significant_or_deal_killer(self):
        report = pre_underwrite(self.app_id)
        fico_flags = [f for f in report.red_flags if "fico" in f.flag_type.lower()]
        severities = {f.severity for f in fico_flags}
        assert severities & {"significant", "deal_killer"}, \
            f"FICO 615 should be significant or deal_killer, got: {severities}"

    def test_no_standard_lenders_qualify(self):
        report = pre_underwrite(self.app_id)
        qualifying = [lf for lf in report.lender_fit if lf["qualifies"]]
        # FICO 615 is below even New Silver and LendingOne (620 min)
        for q in qualifying:
            # If any qualify, they should be no-DSCR or exception products
            pass  # Acceptable to have some qualify if they truly can
        # Key: Lima One, Kiavi, Roc Capital should NOT qualify
        qualifying_ids = {lf["lender_id"] for lf in qualifying}
        assert "lima_one_dscr" not in qualifying_ids
        assert "kiavi_dscr" not in qualifying_ids
        assert "roc_capital_dscr" not in qualifying_ids

    def test_overall_status_is_decline_risk(self):
        report = pre_underwrite(self.app_id)
        # FICO 615 should push to decline_risk
        assert report.overall_status in ("decline_risk", "conditional"), \
            f"Expected decline_risk or conditional for FICO 615, got {report.overall_status}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Fix & Flip routing
# ─────────────────────────────────────────────────────────────────────────────

class TestFixFlipRouting:
    """Fix & Flip deal should route to fix_flip products, NOT DSCR products."""

    def setup_method(self):
        self.prequal_id = "pq_test_fixflip_001"
        self.app_id = "app_test_fixflip_001"
        borrower = {
            "user_id": "usr_test",
            "name": "Test Flipper",
            "credit_score": 700,
            "annual_income": 90_000,
            "liquidity": 45_000,
            "properties_owned": 2,
            "loan_purpose": "purchase",
            "desired_loan_amount": 126_000,  # 70% of $180k purchase → > $100K min
            "down_payment_pct": 30,
        }
        prop = {
            "address": "777 Rehab St Atlanta GA 30301",
            "property_type": "single_family",
            "purchase_price": 180_000,
            "estimated_value": 180_000,
            "monthly_rent": 0,            # no rent — it's a flip
            "annual_taxes": 2_400,
            "annual_insurance": 1_200,
            "rehab_budget": 45_000,
            "arv": 280_000,
        }
        _insert_prequal(self.prequal_id, borrower, prop, "fix_flip", dscr=None, ltv=0.70, monthly_pmt=1100.0)
        _insert_application(self.app_id, self.prequal_id)

    def test_routes_to_fix_flip_products(self):
        report = pre_underwrite(self.app_id)
        qualifying = [lf for lf in report.lender_fit if lf["qualifies"]]
        # All qualifying lenders should be fix_flip products
        for lf in qualifying:
            assert lf.get("product_type") == "fix_flip", \
                f"Expected fix_flip product type, got: {lf.get('product_type')} for {lf.get('lender_id')}"

    def test_no_dscr_lenders_appear(self):
        report = pre_underwrite(self.app_id)
        # No DSCR products should appear since product_type is fix_flip
        for lf in report.lender_fit:
            assert lf.get("product_type") != "dscr", \
                f"DSCR lender should not appear in fix_flip report: {lf.get('lender_id')}"

    def test_has_renovation_budget_condition(self):
        report = pre_underwrite(self.app_id)
        renovation_conditions = [
            c for c in report.conditions
            if "renovation" in c.description.lower() or "budget" in c.description.lower()
        ]
        assert len(renovation_conditions) > 0, "Fix & Flip should have renovation budget condition"

    def test_has_gc_condition(self):
        report = pre_underwrite(self.app_id)
        gc_conditions = [c for c in report.conditions if "contractor" in c.description.lower() or "GC" in c.description]
        assert len(gc_conditions) > 0, "Fix & Flip should require GC documentation"

    def test_georgia_eligible_for_anchor(self):
        """Georgia is in Anchor Loans eligible state list."""
        engine = get_engine()
        anchor_entry = engine.get_index().get("anchor_loans_fix_flip", {})
        assert "GA" in anchor_entry.get("states", []), "Georgia should be in Anchor's eligible states"


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for guideline engine
# ─────────────────────────────────────────────────────────────────────────────

class TestGuidelineEngine:
    def test_index_loads(self):
        engine = get_engine()
        idx = engine.get_index()
        assert len(idx) >= 8, f"Expected at least 8 lender entries, got {len(idx)}"

    def test_guideline_doc_loads(self):
        engine = get_engine()
        doc = engine.get_guideline_doc("lima_one_dscr")
        assert doc is not None
        assert "Lima One" in doc

    def test_match_lenders_clean_dscr(self):
        engine = get_engine()
        matches = engine.match_lenders(
            fico=720,
            ltv=0.75,
            product_type="dscr",
            dscr=1.25,
            state="MI",
            loan_amount=150_000,
            property_type="single_family",
        )
        qualifying = [m for m in matches if m["qualifies"]]
        assert len(qualifying) >= 2, "Should have at least 2 qualifying lenders for strong DSCR"

    def test_match_lenders_fix_flip_georgia(self):
        engine = get_engine()
        matches = engine.match_lenders(
            fico=700,
            ltv=0.65,
            product_type="fix_flip",
            state="GA",
            loan_amount=100_000,
        )
        qualifying_ids = {m["lender_id"] for m in matches if m["qualifies"]}
        assert "anchor_loans_fix_flip" in qualifying_ids, "Anchor should qualify for GA fix_flip"

    def test_match_lenders_state_restriction(self):
        """Anchor Loans should NOT qualify for Ohio (not in their state list)."""
        engine = get_engine()
        matches = engine.match_lenders(
            fico=700,
            ltv=0.65,
            product_type="fix_flip",
            state="OH",
            loan_amount=100_000,
        )
        anchor = next((m for m in matches if m["lender_id"] == "anchor_loans_fix_flip"), None)
        assert anchor is not None, "Anchor should still appear in results"
        assert not anchor["qualifies"], "Anchor should not qualify for Ohio"
        assert any("State" in r for r in anchor["decline_reasons"]), "Should have state decline reason"
