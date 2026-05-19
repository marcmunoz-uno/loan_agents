"""
loan_officer/tests/test_arive_create_loan.py — Arive create_loan wrapper.
No network: ZapierMCPClient.execute is patched.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from loan_officer.arive_create_loan import (
    split_name,
    parse_address,
    map_to_arive_params,
    create_loan_in_arive,
    _extract_arive_loan_id,
    PROPERTY_TYPE_MAP,
)


# ── Name splitting ────────────────────────────────────────────────────────────

def test_split_name_simple():
    assert split_name("Marc Munoz") == ("Marc", "Munoz")


def test_split_name_single():
    assert split_name("Cher") == ("Cher", "")


def test_split_name_empty():
    assert split_name("") == ("", "")


def test_split_name_llc_with_title():
    """The PSA extracted-name case: Michael Sonagiers is the human."""
    out = split_name("Michael Sonagiers, President, Sonagiers, LLC")
    assert out == ("Michael", "Sonagiers")


def test_split_name_middle_kept_as_first():
    """First-name field absorbs middle names; last name is the final token."""
    out = split_name("John Q Public")
    assert out == ("John Q", "Public")


# ── Address parsing ──────────────────────────────────────────────────────────

def test_parse_address_full():
    addr = parse_address("3418 E 121st St, Cleveland, Cuyahoga County, OH 44120")
    assert addr["addressLineText"] == "3418 E 121st St"
    assert addr["city"] == "Cleveland"
    assert addr["county"] == "Cuyahoga County"
    assert addr["state"] == "OH"
    assert addr["postalCode"] == "44120"


def test_parse_address_no_county():
    addr = parse_address("4521 Oak Ln, Detroit, MI 48224")
    assert addr["addressLineText"] == "4521 Oak Ln"
    assert addr["city"] == "Detroit"
    assert addr["state"] == "MI"
    assert addr["postalCode"] == "48224"
    assert addr["county"] == ""


def test_parse_address_zip_plus_4():
    addr = parse_address("1 Main St, Anytown, NY 12345-6789")
    assert addr["state"] == "NY"
    assert addr["postalCode"] == "12345"


def test_parse_address_empty():
    addr = parse_address("")
    assert addr == {"addressLineText": "", "city": "", "county": "", "state": "", "postalCode": ""}


def test_parse_address_malformed_doesnt_raise():
    addr = parse_address("just a street name no commas")
    assert addr["addressLineText"] == "just a street name no commas"
    assert addr["state"] == ""


# ── Param mapping ────────────────────────────────────────────────────────────

def _prequal(
    name="Marc Munoz", email="marc@munoz.ltd", phone="(917) 981-0032",
    fico=740, monthly_income=10000, annual_income=120000,
    desired_loan_amount=72000, property_type="single_family",
    property_address="4521 Oak Ln Detroit MI 48224",
    purchase_price=100000, prequal_id="pq_test_42",
):
    return {
        "id": prequal_id,
        "user_id": "usr_test",
        "borrower_data": {
            "name": name, "email": email, "phone": phone,
            "credit_score": fico, "monthly_income": monthly_income,
            "annual_income": annual_income, "desired_loan_amount": desired_loan_amount,
        },
        "property_data": {
            "address": property_address, "property_type": property_type,
            "purchase_price": purchase_price,
        },
    }


def test_map_required_fields_present():
    p = map_to_arive_params(_prequal(), {})
    assert p["loanPurpose"] == "Purchase"
    assert p["originatorEmail"]
    assert p["borrower1_firstName"] == "Marc"
    assert p["borrower1_lastName"] == "Munoz"
    assert p["borrower1_emailAddressText"] == "marc@munoz.ltd"


def test_map_dscr_defaults():
    p = map_to_arive_params(_prequal(), {})
    assert p["mortgageType"] == "NonQM"
    assert p["propertyUsageType"] == "Investment"
    assert p["homebuyingStage"] == "UNDER_CONTRACT"


def test_map_estimated_fico_to_string():
    p = map_to_arive_params(_prequal(fico=720), {})
    assert p["estimatedFICO"] == "720"


def test_map_estimated_fico_omits_when_unknown():
    pre = _prequal()
    pre["borrower_data"].pop("credit_score")
    p = map_to_arive_params(pre, {})
    assert "estimatedFICO" not in p


def test_map_base_loan_amount_from_desired():
    p = map_to_arive_params(_prequal(desired_loan_amount=72500), {})
    assert p["baseLoanAmount"] == 72500.0


def test_map_base_loan_amount_default_75_pct():
    pre = _prequal(desired_loan_amount=0, purchase_price=100000)
    p = map_to_arive_params(pre, {})
    assert p["baseLoanAmount"] == 75000.0


def test_map_purchase_price_from_psa_overrides_prequal():
    """PSA's purchase price wins over the prequal's estimate."""
    pre = _prequal(purchase_price=100000)
    p = map_to_arive_params(pre, {"purchase_price": "$105,000.00"})
    assert p["purchasePriceOrEstimatedValue"] == 105000.0


def test_map_subject_property_address_components():
    p = map_to_arive_params(
        _prequal(),
        {"property_address": "3418 E 121st St, Cleveland, Cuyahoga County, OH 44120"},
    )
    assert p["subjectProperty_addressLineText"] == "3418 E 121st St"
    assert p["subjectProperty_city"] == "Cleveland"
    assert p["subjectProperty_state"] == "OH"
    assert p["subjectProperty_postalCode"] == "44120"
    assert p["subjectProperty_county"] == "Cuyahoga County"


def test_map_property_type_lookup():
    for src, expected in [
        ("single_family", "SINGLE_FAMILY_DETACHED"),
        ("duplex",        "TWO_UNIT"),
        ("condo",         "CONDO_UNDER_5_STORIES"),
        ("townhouse",     "TOWNHOUSE"),
        ("commercial",    "MIXED_USE_PROPERTY"),
    ]:
        p = map_to_arive_params(_prequal(property_type=src), {})
        assert p["propertyType"] == expected, f"property_type={src!r}"


def test_map_phone_strips_to_10_digits():
    p = map_to_arive_params(_prequal(phone="(917) 981-0032"), {})
    assert p["borrower1_mobilePhone10digit"] == "9179810032"


def test_map_phone_omits_when_too_short():
    p = map_to_arive_params(_prequal(phone="555-12"), {})
    assert "borrower1_mobilePhone10digit" not in p


def test_map_monthly_income_from_monthly_or_annual():
    p1 = map_to_arive_params(_prequal(monthly_income=10000, annual_income=0), {})
    assert p1["borrower1_totalMonthlyIncome"] == 10000.0

    pre2 = _prequal(monthly_income=0, annual_income=120000)
    p2 = map_to_arive_params(pre2, {})
    assert p2["borrower1_totalMonthlyIncome"] == 10000.0


def test_map_external_create_date_is_today():
    from datetime import datetime, timezone
    p = map_to_arive_params(_prequal(), {})
    assert p["externalCreateDate"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_map_crm_reference_id_is_prequal_id():
    p = map_to_arive_params(_prequal(prequal_id="pq_test_xyz"), {})
    assert p["crmReferenceId"] == "pq_test_xyz"


def test_map_originator_email_override():
    p = map_to_arive_params(_prequal(), {}, originator_email="other@example.com")
    assert p["originatorEmail"] == "other@example.com"


def test_map_llc_buyer_name_extracts_president():
    """Real PSA had buyer 'Michael Sonagiers, President, Sonagiers, LLC' —
    we should use the human's name, not the LLC."""
    pre = _prequal(name="Michael Sonagiers, President, Sonagiers, LLC")
    p = map_to_arive_params(pre, {})
    assert p["borrower1_firstName"] == "Michael"
    assert p["borrower1_lastName"] == "Sonagiers"


def test_map_omits_empty_address_components():
    pre = _prequal(property_address="")
    p = map_to_arive_params(pre, {"property_address": ""})
    assert "subjectProperty_addressLineText" not in p


# ── create_loan_in_arive — execution path ─────────────────────────────────────

def test_create_loan_skips_when_mcp_unconfigured():
    fake = type("MCP", (), {"configured": False,
                            "execute": lambda self, **kw: (_ for _ in ()).throw(AssertionError("must not be called"))})()
    with patch("loan_officer.arive_create_loan.ZapierMCPClient", return_value=fake):
        out = create_loan_in_arive(_prequal(), {"purchase_price": 100000})
    assert out["ok"] is False
    assert out["status"] == "skipped:zapier_mcp_not_configured"
    assert out["arive_loan_id"] == ""


def test_create_loan_calls_mcp_with_arive_action():
    fake = MagicMock()
    fake.configured = True
    fake.execute.return_value = {
        "isError": False,
        "content": [{"type": "text", "text": '{"loan_id": "arv_abc12345", "status": "created"}'}],
    }
    with patch("loan_officer.arive_create_loan.ZapierMCPClient", return_value=fake):
        out = create_loan_in_arive(_prequal(), {"purchase_price": "$100,000"})

    fake.execute.assert_called_once()
    kwargs = fake.execute.call_args.kwargs
    assert kwargs["app"] == "arive"
    assert kwargs["action"] == "create_loan"
    assert kwargs["mode"] == "write"
    p = kwargs["params"]
    # required fields landed
    assert p["loanPurpose"] == "Purchase"
    assert p["borrower1_firstName"] == "Marc"
    assert p["originatorEmail"]
    # DSCR defaults
    assert p["mortgageType"] == "NonQM"
    assert out["ok"] is True
    assert out["status"] == "sent"
    assert out["arive_loan_id"] == "arv_abc12345"


def test_create_loan_extracts_loan_id_from_id_field():
    fake = MagicMock()
    fake.configured = True
    fake.execute.return_value = {
        "isError": False,
        "content": [{"type": "text", "text": '{"id": "12345678", "borrower_email": "x@y"}'}],
    }
    with patch("loan_officer.arive_create_loan.ZapierMCPClient", return_value=fake):
        out = create_loan_in_arive(_prequal(), {})
    assert out["arive_loan_id"] == "12345678"


def test_create_loan_handles_mcp_failure():
    fake = MagicMock()
    fake.configured = True
    fake.execute.side_effect = RuntimeError("Arive 5xx")
    with patch("loan_officer.arive_create_loan.ZapierMCPClient", return_value=fake):
        out = create_loan_in_arive(_prequal(), {})
    assert out["ok"] is False
    assert out["status"].startswith("failed:")
    assert "Arive 5xx" in out["status"]
    assert out["params_sent"]  # we recorded what we'd have sent for audit
