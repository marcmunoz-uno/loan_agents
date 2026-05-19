"""
loan_officer/tests/test_intake_completeness.py — Pure-logic tests for completeness.py.
"""

from __future__ import annotations

import pytest

from loan_officer.intake.completeness import (
    check_completeness,
    get_checklist,
    gap_message,
)


def test_checklists_exist_for_all_products():
    for product in ("dscr", "fix_flip", "bridge"):
        cl = get_checklist(product)
        assert cl, f"empty checklist for {product}"
        for item in cl:
            assert "doc_type" in item and "required" in item


def test_unknown_product_falls_back_to_dscr():
    cl = get_checklist("totally_made_up")
    assert cl == get_checklist("dscr")


def test_check_completeness_all_required_missing():
    report = check_completeness("dscr", [])
    assert not report.is_complete
    assert "rent_roll" in report.required_missing
    assert "bank_stmt" in report.required_missing
    assert "purchase_contract" in report.required_missing
    assert report.completion_pct == 0.0


def test_check_completeness_all_required_met():
    received = ["rent_roll", "bank_stmt", "purchase_contract"]
    report = check_completeness("dscr", received)
    assert report.is_complete
    assert report.required_missing == []
    assert report.completion_pct == 100.0


def test_check_completeness_partial_required():
    report = check_completeness("dscr", ["rent_roll", "bank_stmt"])
    assert not report.is_complete
    assert report.required_missing == ["purchase_contract"]
    assert report.completion_pct == pytest.approx(66.7, rel=0.01)


def test_check_completeness_tracks_optional_missing():
    received = ["rent_roll", "bank_stmt", "purchase_contract"]
    report = check_completeness("dscr", received)
    assert set(report.optional_missing) == {"appraisal", "insurance_binder", "entity_docs"}


def test_check_completeness_dedupes_received():
    report = check_completeness("dscr", ["rent_roll", "rent_roll", "bank_stmt", "purchase_contract"])
    assert report.is_complete
    assert sorted(report.received) == ["bank_stmt", "purchase_contract", "rent_roll"]


def test_gap_message_complete():
    report = check_completeness("dscr", ["rent_roll", "bank_stmt", "purchase_contract"])
    assert gap_message(report) == "All required documents received."


def test_gap_message_missing_includes_types():
    report = check_completeness("dscr", ["rent_roll"])
    msg = gap_message(report)
    assert "Still need" in msg
    assert "bank_stmt" in msg
    assert "purchase_contract" in msg
