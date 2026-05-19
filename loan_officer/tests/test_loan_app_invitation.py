"""
loan_officer/tests/test_loan_app_invitation.py — 1003 registration-link email.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from loan_officer.loan_app_invitation import (
    render_invitation_html,
    send_loan_app_invitation,
    DEFAULT_APP_URL,
)


# ── Rendering ────────────────────────────────────────────────────────────────

def test_render_includes_app_url():
    html = render_invitation_html(
        borrower_name="Marc",
        property_address="3418 E 121st St Cleveland OH",
        purchase_price=105000.0,
        closing_date="2026-04-13",
    )
    assert DEFAULT_APP_URL in html
    assert "Complete Your Loan Application" in html


def test_render_uses_override_url():
    html = render_invitation_html(
        borrower_name="X", property_address="", purchase_price=None,
        closing_date="", app_url="https://example.test/app/register",
    )
    assert "https://example.test/app/register" in html
    assert DEFAULT_APP_URL not in html


def test_render_money_format():
    html = render_invitation_html(
        borrower_name="Marc", property_address="123 Main",
        purchase_price=1234500.0, closing_date="2026-12-01",
    )
    assert "$1,234,500" in html


def test_render_falls_back_when_address_empty():
    html = render_invitation_html(
        borrower_name="Marc", property_address="",
        purchase_price=100000.0, closing_date="",
    )
    assert "the subject property" in html
    assert "the agreed closing date" in html


# ── Send via Zapier MCP ──────────────────────────────────────────────────────

def test_send_skips_when_no_borrower_email():
    out = send_loan_app_invitation(
        borrower_name="Marc", borrower_email="",
        property_address="x", purchase_price=100000.0, closing_date="2026-01-01",
    )
    assert out["ok"] is False
    assert out["status"] == "skipped:no_borrower_email"


def test_send_skips_when_mcp_unconfigured():
    fake = type("MCP", (), {"configured": False,
                            "execute": lambda self, **kw: (_ for _ in ()).throw(AssertionError("must not be called"))})()
    with patch("loan_officer.loan_app_invitation.ZapierMCPClient", return_value=fake):
        out = send_loan_app_invitation(
            borrower_name="Marc", borrower_email="m@x.com",
            property_address="x", purchase_price=1.0, closing_date="2026-01-01",
        )
    assert out["ok"] is False
    assert out["status"] == "skipped:zapier_mcp_not_configured"


def test_send_calls_mcp_with_gmail_message():
    fake = MagicMock()
    fake.configured = True
    fake.execute.return_value = {"isError": False, "content": []}
    with patch("loan_officer.loan_app_invitation.ZapierMCPClient", return_value=fake):
        out = send_loan_app_invitation(
            borrower_name="Marc Munoz", borrower_email="marc@munoz.ltd",
            property_address="3418 E 121st St Cleveland OH",
            purchase_price=105000.0, closing_date="2026-04-13",
        )
    assert out["ok"] is True
    assert out["status"] == "sent"
    assert out["sent_to"] == "marc@munoz.ltd"
    assert out["app_url"] == DEFAULT_APP_URL
    fake.execute.assert_called_once()
    kwargs = fake.execute.call_args.kwargs
    assert kwargs["app"] == "gmail"
    assert kwargs["action"] == "message"
    p = kwargs["params"]
    assert p["to"] == ["marc@munoz.ltd"]
    assert "loan application" in p["subject"].lower()
    assert DEFAULT_APP_URL in p["body"]
    assert p["body_type"] == "html"


def test_send_handles_mcp_failure():
    fake = MagicMock()
    fake.configured = True
    fake.execute.side_effect = RuntimeError("MCP 500")
    with patch("loan_officer.loan_app_invitation.ZapierMCPClient", return_value=fake):
        out = send_loan_app_invitation(
            borrower_name="Marc", borrower_email="m@x.com",
            property_address="x", purchase_price=1.0, closing_date="2026-01-01",
        )
    assert out["ok"] is False
    assert out["status"].startswith("failed:")
    assert "MCP 500" in out["status"]
    assert out["sent_to"] == ""


def test_send_respects_app_url_override():
    fake = MagicMock()
    fake.configured = True
    fake.execute.return_value = {"isError": False, "content": []}
    with patch("loan_officer.loan_app_invitation.ZapierMCPClient", return_value=fake):
        out = send_loan_app_invitation(
            borrower_name="X", borrower_email="x@y.com",
            property_address="z", purchase_price=1.0, closing_date="2026-01-01",
            app_url="https://custom.example.com/loan/start",
        )
    assert out["app_url"] == "https://custom.example.com/loan/start"
    kwargs = fake.execute.call_args.kwargs
    assert "https://custom.example.com/loan/start" in kwargs["params"]["body"]
