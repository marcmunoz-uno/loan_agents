"""
loan_officer/tests/test_workflow.py — State machine smoke tests.

Run: python -m pytest loan_officer/tests/ -v
"""

import pytest
from loan_officer.workflows import (
    transition, add_audit_event, validate_transition,
    parse_audit_log, state_summary, STATES, TRANSITIONS,
)
from loan_officer.prequal import compute_dscr, compute_ltv, _monthly_payment
from loan_officer.lender_router import suggest_product
from shared.schemas import BorrowerProfile, PropertyProfile


# ── Workflow state machine tests ──────────────────────────────────────────────

class TestStateMachine:
    def test_valid_transition_new_to_prequal(self):
        state, log = transition("NEW", "PREQUAL_PENDING", [])
        assert state == "PREQUAL_PENDING"
        assert len(log) == 1
        assert log[0]["from_state"] == "NEW"
        assert log[0]["to_state"] == "PREQUAL_PENDING"

    def test_valid_full_approval_path(self):
        """Walk the happy path all the way to FUNDED."""
        log = []
        state = "NEW"
        path = ["PREQUAL_SCORED", "APP_STARTED", "APP_SUBMITTED", "UNDERWRITING", "APPROVED", "CLOSING", "FUNDED"]
        for next_state in path:
            state, log = transition(state, next_state, log)
        assert state == "FUNDED"
        assert len(log) == len(path)

    def test_invalid_transition_raises(self):
        with pytest.raises(ValueError, match="Invalid transition"):
            validate_transition("NEW", "FUNDED")

    def test_terminal_state_no_transitions(self):
        allowed = TRANSITIONS.get("FUNDED", set())
        assert len(allowed) == 0

    def test_decline_from_underwriting(self):
        state, log = transition("UNDERWRITING", "DECLINED", [])
        assert state == "DECLINED"

    def test_conditions_loop_back(self):
        """Conditions can go back to underwriting or directly to approved."""
        state, log = transition("UNDERWRITING", "CONDITIONS", [])
        state, log = transition(state, "UNDERWRITING", log)
        state, log = transition(state, "APPROVED", log)
        assert state == "APPROVED"

    def test_audit_log_accumulates(self):
        log = []
        state = "NEW"
        _, log = transition(state, "PREQUAL_SCORED", log)
        log = add_audit_event(log, "note_added", {"text": "Borrower called."})
        _, log = transition("PREQUAL_SCORED", "APP_STARTED", log)
        assert len(log) == 3
        assert log[1]["event_type"] == "note_added"

    def test_parse_audit_log_handles_string(self):
        import json
        events = [{"event_type": "test", "ts": "2026-01-01"}]
        parsed = parse_audit_log(json.dumps(events))
        assert parsed == events

    def test_parse_audit_log_handles_empty(self):
        assert parse_audit_log("") == []
        assert parse_audit_log(None) == []

    def test_state_summary(self):
        s = state_summary("UNDERWRITING")
        assert s["state"] == "UNDERWRITING"
        assert not s["is_terminal"]
        assert "APPROVED" in s["allowed_next"]


# ── Prequal scoring tests ─────────────────────────────────────────────────────

class TestPrequal:
    def test_monthly_payment_calculation(self):
        # $200k, 8%, 30yr ≈ $1468/mo
        pmt = _monthly_payment(200_000, 8.0, 360)
        assert 1400 < pmt < 1550

    def test_dscr_strong_deal(self):
        # $1200/mo rent, $650/mo payment, $2400/yr taxes, $1200/yr insurance
        dscr = compute_dscr(
            monthly_rent=1200,
            monthly_payment=650,
            annual_taxes=2400,
            annual_insurance=1200,
        )
        assert dscr > 1.0

    def test_dscr_below_one(self):
        # Rent too low relative to payment
        dscr = compute_dscr(
            monthly_rent=500,
            monthly_payment=1500,
            annual_taxes=3000,
            annual_insurance=1500,
        )
        assert dscr < 1.0

    def test_ltv_calculation(self):
        ltv = compute_ltv(72_000, 95_000)
        assert abs(ltv - 0.758) < 0.01


# ── Lender router tests ───────────────────────────────────────────────────────

class TestLenderRouter:
    def _make_borrower(self, **kwargs) -> BorrowerProfile:
        defaults = {
            "user_id": "usr_test",
            "name": "Test Investor",
            "credit_score": 720,
            "annual_income": 100_000,
            "liquidity": 50_000,
            "properties_owned": 2,
            "loan_purpose": "purchase",
            "desired_loan_amount": 75_000,
            "down_payment_pct": 25,
        }
        defaults.update(kwargs)
        return BorrowerProfile(**defaults)

    def _make_property(self, **kwargs) -> PropertyProfile:
        defaults = {
            "address": "4521 Oak Ln Detroit MI",
            "property_type": "single_family",
            "purchase_price": 95_000,
            "estimated_value": 110_000,
            "monthly_rent": 1200,
            "annual_taxes": 2400,
            "annual_insurance": 1200,
        }
        defaults.update(kwargs)
        return PropertyProfile(**defaults)

    def test_dscr_for_rental_property(self):
        result = suggest_product(self._make_borrower(), self._make_property())
        assert result["product"] == "dscr"

    def test_fix_flip_for_rehab_deal(self):
        b = self._make_borrower(loan_purpose="purchase")
        p = self._make_property(
            monthly_rent=0,
            rehab_budget=25_000,
            arv=150_000,
            purchase_price=80_000,
        )
        result = suggest_product(b, p)
        assert result["product"] in ("fix_flip", "dscr")  # fix_flip should rank high

    def test_result_has_required_keys(self):
        result = suggest_product(self._make_borrower(), self._make_property())
        for key in ("product", "fit_score", "qualifies", "rationale", "lenders", "alternatives"):
            assert key in result, f"Missing key: {key}"

    def test_alternatives_are_returned(self):
        result = suggest_product(self._make_borrower(), self._make_property())
        assert isinstance(result["alternatives"], list)
        assert len(result["alternatives"]) >= 1
