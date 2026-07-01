"""
loan_officer/tests/test_arive_create_lead.py — Typeform intake → Arive lead.

The Arive create_lead action is externally broken, so firing is gated OFF; these
tests cover the mapping (pure) and the gate. No network.
"""

from __future__ import annotations

from loan_officer.arive_create_lead import (
    map_intake_to_lead_params,
    create_lead_from_intake,
)


def _intake(**over):
    base = {
        "first_name": "Daniel",
        "last_name": "Paul",
        "email": "Samdengroup@gmail.com",
        "phone": "+1 (323) 363-2805",
        "credit_score_estimate": 720,
        "primary_residence_status": "own",
        "typeform_response_id": "resp_abc123",
    }
    base.update(over)
    return base


def test_map_intake_sets_required_and_dscr_defaults():
    p = map_intake_to_lead_params(_intake())
    assert p["borrower_firstName"] == "Daniel"
    assert p["borrower_lastName"] == "Paul"
    assert p["borrower_emailAddressText"] == "Samdengroup@gmail.com"
    assert p["loanPurpose"] == "Purchase"
    assert p["assigneeEmail"]  # originator/LO email present
    assert p["mortgageType"] == "NonQM"
    assert p["propertyUsageType"] == "Investment"
    assert p["leadStatus"] == "NEW"
    assert p["estimatedFICO"] == "720"
    assert p["borrower_mobilePhone10digit"] == "3233632805"   # stripped to 10 digits
    assert p["borrower_occupancy"] == "Own"
    assert p["crmReferenceId"] == "resp_abc123"


def test_map_intake_handles_missing_name_and_fico():
    p = map_intake_to_lead_params(_intake(first_name="", last_name="", credit_score_estimate=None, phone=""))
    assert p["borrower_firstName"] == "Borrower"     # default
    assert p["borrower_lastName"]                    # never empty (Arive requires it)
    assert "estimatedFICO" not in p
    assert "borrower_mobilePhone10digit" not in p


def test_create_lead_skipped_when_disabled(monkeypatch):
    monkeypatch.delenv("ARIVE_CREATE_LEADS", raising=False)
    out = create_lead_from_intake(_intake())
    assert out["status"] == "skipped:disabled"
    assert out["ok"] is False


def test_create_lead_enabled_but_zapier_unconfigured(monkeypatch):
    monkeypatch.setenv("ARIVE_CREATE_LEADS", "1")
    monkeypatch.delenv("ZAPIER_MCP_ENDPOINT", raising=False)
    monkeypatch.delenv("ZAPIER_MCP_API_KEY", raising=False)
    out = create_lead_from_intake(_intake())
    assert out["status"] == "skipped:zapier_mcp_not_configured"
