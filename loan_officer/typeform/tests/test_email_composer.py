"""
loan_officer/typeform/tests/test_email_composer.py — Render tests for the
deterministic HTML email body and the compose_email subject mapping.

Run: python -m pytest loan_officer/typeform/tests/test_email_composer.py -v
"""

import pytest

from loan_officer.typeform.email_composer import (
    SIGNER, compose_email, render_intake_email_html,
)


def test_render_includes_firm_letterhead_and_signer():
    html = render_intake_email_html(
        borrower_name="Jane Doe",
        soft_prequal_status="pass",
        decision_reasons=["Excellent estimated credit (780)."],
        missing_required_docs=[],
    )
    assert SIGNER["firm_name"] in html
    assert SIGNER["lo_name"] in html
    assert SIGNER["lo_email"] in html
    assert SIGNER["lo_phone"] in html
    assert SIGNER["firm_address_line_1"] in html
    assert SIGNER["firm_short_name"] in html


def test_pass_status_renders_property_details_request():
    html = render_intake_email_html(
        borrower_name="Jane Doe",
        soft_prequal_status="pass",
        decision_reasons=[],
        missing_required_docs=[],
    )
    assert "conditionally pre-qualified" in html.lower()
    assert "Property address" in html
    assert "Purchase price" in html
    assert "monthly rent" in html.lower()


def test_decline_status_renders_alternatives():
    html = render_intake_email_html(
        borrower_name="Jane Doe",
        soft_prequal_status="decline",
        decision_reasons=["Estimated credit score (580) is below the 620 minimum for DSCR financing."],
        missing_required_docs=[],
    )
    assert "not able to move forward" in html.lower()
    assert "credit repair" in html.lower() or "alternative loan programs" in html.lower()
    assert "(580)" in html  # decision reason included


def test_missing_docs_section_renders_when_any_missing():
    html = render_intake_email_html(
        borrower_name="Jane Doe",
        soft_prequal_status="conditional",
        decision_reasons=[],
        missing_required_docs=["Driver's License (Front)", "Recent 30 days of Asset Statements"],
    )
    assert "Required documents we still need" in html
    assert "Driver's License (Front)" in html
    assert "Recent 30 days of Asset Statements" in html


def test_unknown_status_defaults_to_conditional():
    html = render_intake_email_html(
        borrower_name="Jane Doe",
        soft_prequal_status="bogus",
        decision_reasons=[],
        missing_required_docs=[],
    )
    assert "conditionally pre-qualified" in html.lower()


def test_compose_email_subject_per_status():
    for status, subject_match in [
        ("pass",        "Next: Property Details"),
        ("conditional", "A Few Items to Address"),
        ("decline",     "Update on Your Submission"),
    ]:
        out = compose_email(
            intake={"first_name": "Jane", "last_name": "Doe"},
            soft_prequal={"status": status, "score": 80,
                          "decision_reasons": [], "missing_required_docs": []},
        )
        assert subject_match in out["subject"]
        assert "<div" in out["body"]
        assert "Jane Doe" in out["body"]
