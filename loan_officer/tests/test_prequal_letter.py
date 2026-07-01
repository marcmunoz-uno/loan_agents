"""
loan_officer/tests/test_prequal_letter.py — Math, extraction, rendering, audit.

No external calls: reportlab runs locally; fire_zap is patched so we never
hit a real Zapier webhook. SQLite is the conftest temp_db.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

from shared.db import get_conn, insert
from loan_officer.prequal_letter import (
    UWParams,
    compute_max_pp,
    compute_max_pp_range,
    extract_liquidity_from_intake,
    render_letter_pdf,
    generate_and_send,
    get_letter,
    _round_to_thousands,
    _pick_balance,
    _coerce_money,
)


# ── _coerce_money + _pick_balance ─────────────────────────────────────────────

def test_coerce_money_handles_currency_strings():
    assert _coerce_money("$8,500.00") == 8500.0
    assert _coerce_money(" 1,234 ") == 1234.0
    assert _coerce_money(0) == 0.0
    assert _coerce_money(None) is None
    assert _coerce_money("not money") is None


def test_pick_balance_prefers_canonical_keys():
    assert _pick_balance({"ending_balance": "$8,500"}) == 8500.0
    assert _pick_balance({"available_balance": 1500, "ending_balance": 2000}) == 2000.0
    # Whitelist only — a non-canonical "*balance" field is NOT trusted, since it
    # could be a beginning/minimum/prior-month balance or a different sub-account.
    assert _pick_balance({"prior_month_balance": 999}) is None
    assert _pick_balance({"bank_name": "Chase"}) is None


# ── Rounding ─────────────────────────────────────────────────────────────────

def test_round_to_thousands_floors_for_safety():
    assert _round_to_thousands(80499.99, down=True) == 80000.0
    assert _round_to_thousands(80500, down=True) == 80000.0
    assert _round_to_thousands(80999, down=True) == 80000.0
    assert _round_to_thousands(81000, down=True) == 81000.0


def test_round_to_thousands_zero_or_negative():
    assert _round_to_thousands(0) == 0.0
    assert _round_to_thousands(-50) == 0.0


# ── Math: compute_max_pp ─────────────────────────────────────────────────────

def test_max_pp_grows_with_liquidity():
    a = compute_max_pp(liquid_assets=20_000).max_pp
    b = compute_max_pp(liquid_assets=100_000).max_pp
    assert b > a > 0


def test_max_pp_zero_liquidity():
    assert compute_max_pp(liquid_assets=0).max_pp == 0


def test_max_pp_dscr_binds_when_rent_is_too_low():
    # With $1M liquidity (huge), rent of $500 will bind DSCR
    res = compute_max_pp(liquid_assets=1_000_000, monthly_rent=500)
    assert res.binding_constraint == "dscr"


def test_max_pp_liquidity_binds_when_rent_is_strong():
    # With $20k liquidity and rent of $5,000 (very strong), liquidity binds
    res = compute_max_pp(liquid_assets=20_000, monthly_rent=5_000)
    assert res.binding_constraint == "liquidity"


def test_max_pp_breakdown_fields():
    res = compute_max_pp(liquid_assets=50_000, monthly_rent=1_500)
    assert res.loan_amount_at_max > 0
    assert res.piti_at_max > 0
    assert res.monthly_payment_at_max > 0
    assert res.params["down_pct"] > 0


# ── compute_max_pp_range ─────────────────────────────────────────────────────

def test_range_is_monotonic_and_rounded():
    out = compute_max_pp_range(liquid_assets=25_000, monthly_rent=1_200)
    assert out["max_pp_low"] <= out["max_pp_high"]
    # Both are floored to thousands
    assert out["max_pp_low"] % 1000 == 0
    assert out["max_pp_high"] % 1000 == 0


def test_range_widens_when_dscr_unconstrained():
    """Without rent, only liquidity bounds the upper number — should be larger."""
    with_rent = compute_max_pp_range(liquid_assets=50_000, monthly_rent=1_000)
    no_rent   = compute_max_pp_range(liquid_assets=50_000)
    assert no_rent["max_pp_high"] >= with_rent["max_pp_high"]


def test_range_matches_maya_anton_ballpark():
    """
    Sanity check against the user-supplied template (Maya Anton, $80k max PP).
    Liquidity ~$22-24k should produce a low number around $80k.
    """
    out = compute_max_pp_range(liquid_assets=23_000, monthly_rent=900)
    # Loose ballpark — not asserting exact match, just same order of magnitude
    assert 50_000 <= out["max_pp_low"] <= 130_000


def test_range_ensures_high_above_low_even_when_identical():
    """If liquidity is so small both scenarios collapse to the same number,
    the function still surfaces a >= $5k spread so the letter reads sensibly."""
    out = compute_max_pp_range(liquid_assets=100)
    assert out["max_pp_high"] >= out["max_pp_low"]


# ── extract_liquidity_from_intake ────────────────────────────────────────────

def _seed_intake_doc(application_id, doc_id, classified_doc_type, extracted_fields):
    now = "2026-05-19T00:00:00+00:00"
    with get_conn() as conn:
        insert(conn, "intake_documents", {
            "doc_id":              doc_id,
            "deal_id":             "deal_x",
            "application_id":      application_id,
            "user_id":             "usr_marc",
            "filename":            f"{doc_id}.pdf",
            "content_type":        "application/pdf",
            "s3_bucket":           "test",
            "s3_key":              f"intake/deals/deal_x/docs/{doc_id}/x.pdf",
            "classified_doc_type": classified_doc_type,
            "status":              "classified",
            "extracted_fields":    json.dumps(extracted_fields),
            "created_at":          now,
            "updated_at":          now,
        })


def test_extract_liquidity_sums_bank_stmts(temp_db):
    _seed_intake_doc("app_a", "doc_1", "bank_stmt", {"ending_balance": "$8,500"})
    _seed_intake_doc("app_a", "doc_2", "bank_stmt", {"available_balance": 12_000})
    _seed_intake_doc("app_a", "doc_3", "w2",        {"gross_wages": 90_000})  # non-bank-stmt, skipped

    out = extract_liquidity_from_intake("app_a")
    assert out["liquid_assets"] == 20_500.0
    assert out["num_bank_stmts_used"] == 2
    assert out["num_bank_stmts_skipped"] == 0
    assert len(out["breakdown"]) == 2


def test_extract_liquidity_skips_bank_stmts_without_balance(temp_db):
    _seed_intake_doc("app_b", "doc_1", "bank_stmt", {"bank_name": "Chase"})
    _seed_intake_doc("app_b", "doc_2", "bank_stmt", {"ending_balance": 5_000})

    out = extract_liquidity_from_intake("app_b")
    assert out["liquid_assets"] == 5_000.0
    assert out["num_bank_stmts_used"] == 1
    assert out["num_bank_stmts_skipped"] == 1


def test_extract_liquidity_empty_app(temp_db):
    out = extract_liquidity_from_intake("app_with_no_docs")
    assert out["liquid_assets"] == 0.0


def test_extract_liquidity_dedupes_same_account_across_months(temp_db):
    """Two monthly statements for the SAME account must not be summed — that
    would double-count liquidity and inflate the max purchase price."""
    _seed_intake_doc("app_dd", "doc_1", "bank_stmt",
                     {"bank_name": "Chase", "account_number": "1234567890", "ending_balance": 40_000})
    _seed_intake_doc("app_dd", "doc_2", "bank_stmt",
                     {"bank_name": "Chase", "account_number": "1234567890", "ending_balance": 42_000})
    out = extract_liquidity_from_intake("app_dd")
    assert out["liquid_assets"] == 42_000.0     # latest/largest, not 82,000
    assert out["num_accounts"] == 1


def test_extract_liquidity_skips_implausible_ocr_balance(temp_db):
    """An OCR misread (e.g. account number read as a balance) must be dropped,
    not turned into a multi-million-dollar max purchase price."""
    _seed_intake_doc("app_big", "doc_1", "bank_stmt", {"ending_balance": 99_999_999_999})
    _seed_intake_doc("app_big", "doc_2", "bank_stmt", {"ending_balance": 30_000})
    out = extract_liquidity_from_intake("app_big")
    assert out["liquid_assets"] == 30_000.0
    assert out["num_bank_stmts_skipped"] == 1


# ── PDF rendering ────────────────────────────────────────────────────────────

def test_render_letter_pdf_returns_pdf_bytes():
    pdf = render_letter_pdf(
        borrower_name="Maya Anton",
        borrower_email="maya@example.com",
        max_pp_low=80_000,
        max_pp_high=95_000,
    )
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000  # not empty


def test_render_letter_signer_override_changes_output():
    """
    Two signers should produce different PDFs (reportlab compresses text streams,
    so we can't easily read content back — but byte-level difference proves the
    override flows into rendering.)
    """
    a = render_letter_pdf(
        borrower_name="Test", borrower_email="t@example.com",
        max_pp_low=50_000, max_pp_high=60_000,
        signer={"firm_name": "Firm A", "firm_address_line_1": "1", "firm_address_line_2": "2",
                "lo_name": "Signer A", "lo_title": "Title A", "lo_email": "a@example.com", "lo_phone": "1"},
    )
    b = render_letter_pdf(
        borrower_name="Test", borrower_email="t@example.com",
        max_pp_low=50_000, max_pp_high=60_000,
        signer={"firm_name": "Firm B", "firm_address_line_1": "3", "firm_address_line_2": "4",
                "lo_name": "Signer B", "lo_title": "Title B", "lo_email": "b@example.com", "lo_phone": "2"},
    )
    assert a != b
    assert a.startswith(b"%PDF-") and b.startswith(b"%PDF-")


# ── generate_and_send: end-to-end with mocked Zapier + seeded prequal ────────

def _seed_prequal(prequal_id, borrower_email="m@example.com", liquidity=22_000):
    now = "2026-05-19T00:00:00+00:00"
    with get_conn() as conn:
        insert(conn, "loan_prequals", {
            "id":                       prequal_id,
            "user_id":                  "usr_marc",
            "borrower_data":            json.dumps({
                "user_id":  "usr_marc",
                "name":     "Maya Anton",
                "email":    borrower_email,
                "liquidity": liquidity,
            }),
            "property_data":            json.dumps({
                "address":        "100 Test Ln Detroit MI",
                "property_type":  "single_family",
                "monthly_rent":   900,
                "purchase_price": 95_000,
                "annual_taxes":   2_400,
                "annual_insurance": 1_200,
            }),
            "score":                    72.0,
            "suggested_product":        "dscr",
            "dscr":                     1.05,
            "ltv":                      0.7,
            "monthly_payment_estimate": 500,
            "strengths":                "[]",
            "concerns":                 "[]",
            "next_steps":               "[]",
            "status":                   "scored",
            "notes":                    "",
            "created_at":               now,
            "updated_at":               now,
        })


def test_generate_and_send_writes_audit_and_returns_pdf(temp_db):
    _seed_prequal("pq_test_1")
    with patch("loan_officer.prequal_letter.fire_zap",
               return_value={"success": True, "event_type": "prequal_letter_sent"}) as zap:
        letter = generate_and_send("pq_test_1")

    assert letter.letter_id.startswith("pql_")
    assert letter.max_pp_low > 0 and letter.max_pp_high >= letter.max_pp_low
    assert letter.zap_fired is True
    assert letter.sent_to == "m@example.com"

    # PDF base64 decodes to real PDF bytes
    raw = base64.standard_b64decode(letter.pdf_base64)
    assert raw.startswith(b"%PDF-")

    # Zapier was called exactly once with the letter_id as correlation_id
    zap.assert_called_once()
    args, kwargs = zap.call_args
    assert args[0] == "prequal_letter_sent"
    assert kwargs.get("correlation_id") == letter.letter_id

    # Audit row exists
    row = get_letter(letter.letter_id)
    assert row is not None
    assert row["zap_fired"] == 1
    assert row["sent_to"] == "m@example.com"


def test_generate_and_send_does_not_fire_zap_when_email_missing(temp_db):
    _seed_prequal("pq_no_email", borrower_email="")
    with patch("loan_officer.prequal_letter.fire_zap") as zap:
        letter = generate_and_send("pq_no_email")
    zap.assert_not_called()
    assert letter.zap_fired is False
    assert letter.sent_to == ""


def test_generate_and_send_skip_send_flag(temp_db):
    _seed_prequal("pq_skip")
    with patch("loan_officer.prequal_letter.fire_zap") as zap:
        letter = generate_and_send("pq_skip", skip_send=True)
    zap.assert_not_called()
    assert letter.zap_fired is False


def test_generate_and_send_unknown_prequal_raises(temp_db):
    with pytest.raises(ValueError, match="not found"):
        generate_and_send("pq_does_not_exist")


def test_generate_and_send_below_min_liquid_raises(temp_db):
    """Autonomous callers pass min_liquid; a thin file must never mail a letter."""
    _seed_prequal("pq_thin", liquidity=2_000)
    with pytest.raises(ValueError, match="below minimum"):
        generate_and_send("pq_thin", min_liquid=5_000.0, skip_send=True)


def test_generate_and_send_zero_max_pp_raises(temp_db):
    """$0 liquidity floors max purchase price at $0 — refuse rather than email
    a "$0 – $5,000" letter on the firm's letterhead."""
    _seed_prequal("pq_zero", liquidity=0)
    with pytest.raises(ValueError, match="max purchase price is \\$0"):
        generate_and_send("pq_zero", liquid_assets_override=0.0, skip_send=True)


def test_generate_and_send_uses_liquidity_override(temp_db):
    _seed_prequal("pq_override", liquidity=5_000)  # tiny self-reported
    with patch("loan_officer.prequal_letter.fire_zap",
               return_value={"success": True}):
        letter_self    = generate_and_send("pq_override")
        letter_overrid = generate_and_send("pq_override", liquid_assets_override=200_000, skip_send=True)
    # Override → bigger PP
    assert letter_overrid.max_pp_high > letter_self.max_pp_high


# ── S3 upload + Zapier MCP send paths ────────────────────────────────────────

def test_generate_and_send_uploads_to_s3_when_configured(temp_db, stub_s3):
    """When S3 is configured, the PDF is uploaded and a presigned URL is included."""
    _seed_prequal("pq_s3")
    with patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        letter = generate_and_send("pq_s3", skip_send=True)

    assert letter.pdf_url == "https://test-bucket.s3.amazonaws.com/signed-get"
    assert letter.pdf_url_expires_at
    stub_s3.put_object_bytes.assert_called_once()
    args, kwargs = stub_s3.put_object_bytes.call_args
    assert kwargs["s3_key"] == f"prequal-letters/{letter.letter_id}.pdf"
    assert kwargs["content_type"] == "application/pdf"


def test_generate_and_send_no_s3_falls_back_to_self_hosted_url(temp_db, monkeypatch):
    """When S3 is unconfigured, the PDF URL falls back to the self-hosted /pdf endpoint."""
    from shared.s3_client import S3Client
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://loan-agents.test")
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-secret")
    bare = S3Client(bucket="")
    _seed_prequal("pq_no_s3")
    with patch("loan_officer.prequal_letter.get_default_client", return_value=bare), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        letter = generate_and_send("pq_no_s3", skip_send=True)

    assert letter.pdf_url.startswith("https://loan-agents.test/api/loan/prequal-letter/")
    assert "/pdf?token=" in letter.pdf_url
    assert letter.pdf_url_expires_at != ""


def test_generate_and_send_s3_upload_failure_falls_back_to_self_hosted(temp_db, stub_s3, monkeypatch):
    """If S3 upload throws, the letter still generates and falls back to the self-hosted URL."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://loan-agents.test")
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-secret")
    stub_s3.put_object_bytes.side_effect = RuntimeError("S3 down")
    _seed_prequal("pq_s3_fail")
    with patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        letter = generate_and_send("pq_s3_fail", skip_send=True)

    assert letter.letter_id.startswith("pql_")
    # Fell back to self-hosted URL
    assert letter.pdf_url.startswith("https://loan-agents.test/api/loan/prequal-letter/")


def test_generate_and_send_uses_mcp_when_configured(temp_db, stub_s3):
    """Server-side Zapier MCP is the preferred path when configured."""
    _seed_prequal("pq_mcp")
    fake_mcp = type("MCP", (), {
        "configured": True,
        "execute": lambda self, **kw: {"isError": False, "content": []},
    })()
    with patch("loan_officer.prequal_letter.ZapierMCPClient", return_value=fake_mcp), \
         patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.fire_zap") as zap:
        letter = generate_and_send("pq_mcp")

    assert letter.mcp_send_status == "sent"
    assert letter.zap_fired is True
    # MCP succeeded → webhook fallback should NOT fire
    zap.assert_not_called()


def test_generate_and_send_falls_back_to_webhook_when_mcp_unconfigured(temp_db, stub_s3):
    """When MCP isn't set, fall through to the existing webhook path."""
    _seed_prequal("pq_fb")
    fake_mcp = type("MCP", (), {
        "configured": False,
        "execute": lambda self, **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
    })()
    with patch("loan_officer.prequal_letter.ZapierMCPClient", return_value=fake_mcp), \
         patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}) as zap:
        letter = generate_and_send("pq_fb")

    assert letter.mcp_send_status == "skipped:zapier_mcp_not_configured"
    assert letter.zap_fired is True  # webhook reported success
    zap.assert_called_once()


def test_generate_and_send_mcp_failure_falls_back_to_webhook(temp_db, stub_s3):
    """If MCP is configured but execute raises, we still try the webhook."""
    _seed_prequal("pq_mcp_err")
    fake_mcp = type("MCP", (), {
        "configured": True,
        "execute": lambda self, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    })()
    with patch("loan_officer.prequal_letter.ZapierMCPClient", return_value=fake_mcp), \
         patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}) as zap:
        letter = generate_and_send("pq_mcp_err")

    assert letter.mcp_send_status.startswith("failed:")
    zap.assert_called_once()


# ── HMAC + self-hosted PDF URL ────────────────────────────────────────────────

def test_sign_and_verify_letter_pdf_token_roundtrip(monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-shared-secret")
    import loan_officer.prequal_letter as pl
    exp = int(__import__("time").time()) + 3600
    tok = pl.sign_letter_pdf_token("pql_x", exp)
    assert pl.verify_letter_pdf_token("pql_x", exp, tok)


def test_verify_letter_pdf_token_rejects_tampered_id(monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-shared-secret")
    import loan_officer.prequal_letter as pl
    exp = int(__import__("time").time()) + 3600
    tok = pl.sign_letter_pdf_token("pql_x", exp)
    assert not pl.verify_letter_pdf_token("pql_other", exp, tok)


def test_verify_letter_pdf_token_rejects_expired(monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-shared-secret")
    import loan_officer.prequal_letter as pl
    exp = int(__import__("time").time()) - 1  # 1s in the past
    tok = pl.sign_letter_pdf_token("pql_x", exp)
    assert not pl.verify_letter_pdf_token("pql_x", exp, tok)


def test_verify_letter_pdf_token_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-shared-secret")
    import loan_officer.prequal_letter as pl
    assert not pl.verify_letter_pdf_token("pql_x", 9999999999, "00" * 16)


def test_build_self_hosted_pdf_url_carries_token_and_exp(monkeypatch):
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-shared-secret")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    from loan_officer.prequal_letter import build_self_hosted_pdf_url
    url = build_self_hosted_pdf_url("pql_y")
    assert url.startswith("https://example.test/api/loan/prequal-letter/pql_y/pdf?")
    assert "token=" in url and "exp=" in url


def test_regenerate_pdf_from_audit_row(temp_db, stub_s3):
    _seed_prequal("pq_regen")
    with patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        letter = generate_and_send("pq_regen", skip_send=True)

    from loan_officer.prequal_letter import regenerate_pdf_from_audit_row
    pdf = regenerate_pdf_from_audit_row(letter.letter_id)
    assert pdf is not None and pdf.startswith(b"%PDF-")


def test_regenerate_pdf_unknown_letter_returns_none(temp_db):
    from loan_officer.prequal_letter import regenerate_pdf_from_audit_row
    assert regenerate_pdf_from_audit_row("pql_missing") is None


def test_generate_and_send_falls_back_to_self_hosted_url_when_no_s3(temp_db, monkeypatch):
    """When S3 is unconfigured, the PDF URL points at our own /pdf endpoint."""
    from shared.s3_client import S3Client
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://loan-agents.test")
    monkeypatch.setenv("TRANCHI_API_SECRET", "test-shared-secret")
    bare = S3Client(bucket="")
    _seed_prequal("pq_selfhost")
    with patch("loan_officer.prequal_letter.get_default_client", return_value=bare), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        letter = generate_and_send("pq_selfhost", skip_send=True)

    assert letter.pdf_url.startswith("https://loan-agents.test/api/loan/prequal-letter/")
    assert "/pdf?token=" in letter.pdf_url
    assert "&exp=" in letter.pdf_url
    assert letter.pdf_url_expires_at != ""


def test_pdf_url_persists_to_audit_row(temp_db, stub_s3):
    _seed_prequal("pq_persist")
    with patch("loan_officer.prequal_letter.get_default_client", return_value=stub_s3), \
         patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        letter = generate_and_send("pq_persist", skip_send=True)

    row = get_letter(letter.letter_id)
    assert row is not None
    assert row["pdf_url"] == letter.pdf_url
    assert row["pdf_url_expires_at"] == letter.pdf_url_expires_at


def test_generate_and_send_uses_intake_docs_over_self_reported(temp_db):
    _seed_prequal("pq_with_intake", liquidity=1_000)
    # Wire an application + bank stmt
    now = "2026-05-19T00:00:00+00:00"
    with get_conn() as conn:
        insert(conn, "loan_applications", {
            "id":                "app_intake_1",
            "prequal_id":        "pq_with_intake",
            "user_id":           "usr_marc",
            "status":            "APP_DOCS_PENDING",
            "current_state":     "APP_DOCS_PENDING",
            "lender_partner":    "",
            "lender_ref_id":     "",
            "docs_required":     "[]",
            "docs_received":     "[]",
            "underwriter_notes": "",
            "approved_amount":   None,
            "approved_rate":     None,
            "approved_term":     None,
            "conditions":        "[]",
            "audit_log":         "[]",
            "created_at":        now,
            "updated_at":        now,
        })
    _seed_intake_doc("app_intake_1", "doc_b1", "bank_stmt", {"ending_balance": 35_000})

    with patch("loan_officer.prequal_letter.fire_zap", return_value={"success": True}):
        letter = generate_and_send("pq_with_intake", skip_send=True)

    assert letter.liquid_assets == 35_000.0
    assert letter.breakdown["intake"]["source"] == "intake_documents"
