"""
loan_officer/prequal_letter.py — Auto-generated pre-qualification letters.

Pipeline:
    1. compute_max_pp_range(liquid, monthly_rent) → conservative + stretch numbers
       binary-searched against liquidity + (optional) DSCR constraints.
    2. extract_liquidity_from_intake(application_id) → sum the
       ending_balance/available_balance values across all classified bank_stmt
       rows in intake_documents.
    3. render_letter_pdf(...) → reportlab-generated PDF bytes matching the
       Munoz, Ghezlan & Co. letterhead.
    4. generate_and_send(prequal_id) → ties it together, writes an audit row
       in prequal_letters, fires the ZAPIER_HOOK_PREQUAL_LETTER_SENT webhook
       so Zapier can attach the PDF + Gmail-send it, returns the PDF inline
       base64 for the caller.

The math is intentionally conservative on the low end and slightly aggressive
on the high end so the borrower sees an honest range, not just an upper bound.
"""

from __future__ import annotations

import base64
import json
import math
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from io import BytesIO
from typing import Any, Optional

from shared.db import get_conn, fetchone, fetchall, insert
from loan_officer.arive_zapier import fire_zap


# ── Underwriting params ──────────────────────────────────────────────────────

@dataclass
class UWParams:
    """Levers for the max-PP solver."""
    down_pct: float = 0.25            # cash down out of PP
    closing_pct: float = 0.03         # buyer-side closing costs out of PP
    reserves_months: int = 6          # months of PITI required in reserves
    annual_rate: float = 0.075        # mortgage rate (decimal)
    amort_months: int = 360           # 30-year amort
    dscr_floor: float = 1.10          # min rent / PITI; ignored if rent unknown
    tax_pct_of_pp: float = 0.025      # annual taxes as % of PP (rough proxy)
    insurance_pct_of_pp: float = 0.012  # annual insurance as % of PP

    # The two scenarios we quote in the range:
    @classmethod
    def conservative(cls) -> "UWParams":
        return cls(
            down_pct=0.25,
            closing_pct=0.03,
            reserves_months=6,
            annual_rate=0.0775,        # closer to the top of the rate band
            dscr_floor=1.10,
        )

    @classmethod
    def stretch(cls) -> "UWParams":
        return cls(
            down_pct=0.20,
            closing_pct=0.03,
            reserves_months=6,
            annual_rate=0.06,          # closer to the bottom of the rate band
            dscr_floor=1.00,
        )


# ── Math: max purchase price ─────────────────────────────────────────────────

def _monthly_payment(loan_amount: float, annual_rate: float, n_months: int) -> float:
    if loan_amount <= 0:
        return 0.0
    r = annual_rate / 12.0
    if r == 0:
        return loan_amount / n_months
    return loan_amount * (r * (1 + r) ** n_months) / ((1 + r) ** n_months - 1)


def _piti_for_pp(pp: float, params: UWParams) -> float:
    """Approx PITI on a hypothetical PP, using per-PP proxies for tax + ins."""
    loan = pp * (1 - params.down_pct)
    p_and_i = _monthly_payment(loan, params.annual_rate, params.amort_months)
    monthly_tax = pp * params.tax_pct_of_pp / 12.0
    monthly_ins = pp * params.insurance_pct_of_pp / 12.0
    return p_and_i + monthly_tax + monthly_ins


def _liquidity_for_pp(pp: float, params: UWParams) -> float:
    """Total cash required to clear closing + meet reserves on a hypothetical PP."""
    piti = _piti_for_pp(pp, params)
    return (
        pp * params.down_pct
        + pp * params.closing_pct
        + piti * params.reserves_months
    )


def _max_pp_by_liquidity(liquid_assets: float, params: UWParams) -> float:
    """Largest PP whose cash-to-close + reserves fits inside available liquidity."""
    if liquid_assets <= 0:
        return 0.0
    lo, hi = 0.0, max(liquid_assets * 20, 50_000)  # generous upper sweep
    for _ in range(64):
        mid = (lo + hi) / 2
        if _liquidity_for_pp(mid, params) <= liquid_assets:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1.0:
            break
    return lo


def _max_pp_by_dscr(monthly_rent: float, params: UWParams) -> float:
    """
    Largest PP at which rent ≥ DSCR_floor × PITI.

    Treats PITI as a monotonic function of PP, then binary-searches.
    """
    if monthly_rent <= 0:
        return float("inf")  # no constraint
    target_piti = monthly_rent / params.dscr_floor
    lo, hi = 0.0, 10_000_000.0
    for _ in range(64):
        mid = (lo + hi) / 2
        if _piti_for_pp(mid, params) <= target_piti:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1.0:
            break
    return lo


@dataclass
class MaxPPResult:
    max_pp: float
    binding_constraint: str        # "liquidity" | "dscr"
    liquid_used: float
    monthly_rent_used: Optional[float]
    piti_at_max: float
    loan_amount_at_max: float
    monthly_payment_at_max: float
    params: dict[str, Any] = field(default_factory=dict)


def compute_max_pp(
    *,
    liquid_assets: float,
    monthly_rent: Optional[float] = None,
    params: Optional[UWParams] = None,
) -> MaxPPResult:
    params = params or UWParams()
    pp_liq = _max_pp_by_liquidity(liquid_assets, params)
    pp_dscr = _max_pp_by_dscr(monthly_rent or 0.0, params) if monthly_rent else float("inf")
    max_pp = min(pp_liq, pp_dscr)
    binding = "liquidity" if pp_liq <= pp_dscr else "dscr"
    piti = _piti_for_pp(max_pp, params)
    loan = max_pp * (1 - params.down_pct)
    return MaxPPResult(
        max_pp=round(max_pp, 2),
        binding_constraint=binding,
        liquid_used=liquid_assets,
        monthly_rent_used=monthly_rent,
        piti_at_max=round(piti, 2),
        loan_amount_at_max=round(loan, 2),
        monthly_payment_at_max=round(_monthly_payment(loan, params.annual_rate, params.amort_months), 2),
        params=asdict(params),
    )


def compute_max_pp_range(
    *,
    liquid_assets: float,
    monthly_rent: Optional[float] = None,
) -> dict[str, Any]:
    """Quote a two-number band: conservative + stretch."""
    cons = compute_max_pp(liquid_assets=liquid_assets, monthly_rent=monthly_rent, params=UWParams.conservative())
    strc = compute_max_pp(liquid_assets=liquid_assets, monthly_rent=monthly_rent, params=UWParams.stretch())
    low = min(cons.max_pp, strc.max_pp)
    high = max(cons.max_pp, strc.max_pp)
    low_rounded = _round_to_thousands(low, down=True)
    high_rounded = _round_to_thousands(high, down=True)
    if high_rounded <= low_rounded:
        high_rounded = low_rounded + 5_000
    return {
        "max_pp_low": low_rounded,
        "max_pp_high": high_rounded,
        "conservative": asdict(cons),
        "stretch": asdict(strc),
    }


def _round_to_thousands(value: float, down: bool = True) -> float:
    """Round to nearest $1,000; floor if down=True so we never overstate."""
    if value <= 0:
        return 0.0
    if down:
        return math.floor(value / 1000.0) * 1000.0
    return math.ceil(value / 1000.0) * 1000.0


# ── Intake liquidity extraction ──────────────────────────────────────────────

LIQUIDITY_FIELD_KEYS = (
    "ending_balance",
    "available_balance",
    "current_balance",
    "balance",
)


def extract_liquidity_from_intake(application_id: str) -> dict[str, Any]:
    """
    Sum the largest extracted balance from each classified bank_stmt row for an
    application. Returns the aggregate + a per-doc breakdown for audit.
    """
    with get_conn() as conn:
        rows = fetchall(
            conn,
            "SELECT doc_id, filename, classified_doc_type, declared_doc_type, "
            "extracted_fields FROM intake_documents WHERE application_id = ?",
            (application_id,),
        )

    breakdown: list[dict[str, Any]] = []
    total = 0.0
    for row in rows:
        doc_type = (row.get("classified_doc_type") or row.get("declared_doc_type") or "").lower()
        if doc_type != "bank_stmt":
            continue
        fields_raw = row.get("extracted_fields") or "{}"
        try:
            fields = json.loads(fields_raw) if isinstance(fields_raw, str) else (fields_raw or {})
        except json.JSONDecodeError:
            fields = {}
        amount = _pick_balance(fields)
        if amount is None:
            breakdown.append({
                "doc_id": row["doc_id"],
                "filename": row.get("filename", ""),
                "balance": None,
                "skipped_reason": "no balance field extracted",
            })
            continue
        total += amount
        breakdown.append({
            "doc_id": row["doc_id"],
            "filename": row.get("filename", ""),
            "balance": amount,
        })
    return {
        "liquid_assets": round(total, 2),
        "num_bank_stmts_used": sum(1 for b in breakdown if b.get("balance") is not None),
        "num_bank_stmts_skipped": sum(1 for b in breakdown if b.get("balance") is None),
        "breakdown": breakdown,
    }


def _pick_balance(fields: dict[str, Any]) -> Optional[float]:
    """Find the most-defensible balance field in an OCR-extracted dict."""
    for key in LIQUIDITY_FIELD_KEYS:
        if key in fields:
            val = _coerce_money(fields[key])
            if val is not None:
                return val
    # Last resort: any field whose name *contains* "balance"
    for k, v in fields.items():
        if "balance" in str(k).lower():
            val = _coerce_money(v)
            if val is not None:
                return val
    return None


def _coerce_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


# ── PDF rendering (reportlab) ────────────────────────────────────────────────

DEFAULT_SIGNER = {
    "firm_name": os.environ.get("LO_FIRM_NAME", "Munoz, Ghezlan & Co., Ltd."),
    "firm_address_line_1": os.environ.get("LO_FIRM_ADDR_1", "99 Wall Street, Suite 4041"),
    "firm_address_line_2": os.environ.get("LO_FIRM_ADDR_2", "New York, NY 10005"),
    "lo_name": os.environ.get("LO_SIGNER_NAME", "Marc Munoz"),
    "lo_title": os.environ.get("LO_SIGNER_TITLE", "Senior Loan Officer"),
    "lo_email": os.environ.get("LO_SIGNER_EMAIL", "marc@munoz.ltd"),
    "lo_phone": os.environ.get("LO_SIGNER_PHONE", "(917) 981-0032"),
}

LETTER_VALIDITY_DAYS = 90


def render_letter_pdf(
    *,
    borrower_name: str,
    borrower_email: str,
    max_pp_low: float,
    max_pp_high: float,
    rate_low_pct: float = 5.875,
    rate_high_pct: float = 8.0,
    down_pct_low: float = 20.0,
    issued_at: Optional[datetime] = None,
    signer: Optional[dict[str, str]] = None,
) -> bytes:
    """
    Render the pre-qualification letter as a PDF.
    Returns the raw bytes; storage is the caller's concern.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
    )
    from reportlab.lib import colors

    signer = signer or DEFAULT_SIGNER
    issued_at = issued_at or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(days=LETTER_VALIDITY_DAYS)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
    )

    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body", parent=styles["Normal"], fontSize=10.5, leading=15, spaceAfter=8,
    )
    small = ParagraphStyle(
        "Small", parent=styles["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#444"),
    )
    head = ParagraphStyle(
        "Head", parent=styles["Heading2"], fontSize=11, leading=14, spaceAfter=4,
        textColor=colors.HexColor("#222"),
    )
    firm = ParagraphStyle(
        "Firm", parent=styles["Heading1"], fontSize=14, leading=16, spaceAfter=2,
        textColor=colors.HexColor("#111"), alignment=1,
    )

    story = []

    # Letterhead (centered)
    story.append(Paragraph(signer["firm_name"], firm))
    story.append(Paragraph(signer["firm_address_line_1"], ParagraphStyle(
        "FirmAddr", parent=small, alignment=1,
    )))
    story.append(Paragraph(signer["firm_address_line_2"], ParagraphStyle(
        "FirmAddr2", parent=small, alignment=1,
    )))
    story.append(Spacer(1, 0.35 * inch))

    # Date + recipient
    story.append(Paragraph(issued_at.strftime("%m/%d/%Y"), body))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(borrower_name, body))
    if borrower_email:
        story.append(Paragraph(borrower_email, body))
    story.append(Spacer(1, 0.2 * inch))

    # Subject
    story.append(Paragraph("<b>Re: Mortgage Pre-Qualification — Non-QM DSCR Loan</b>", body))
    story.append(Spacer(1, 0.15 * inch))

    # Opening
    story.append(Paragraph(f"Dear {borrower_name},", body))
    story.append(Paragraph(
        "Based on our review of the financial documentation you provided, "
        "you are pre-qualified for a Non-QM DSCR Loan under the following parameters:",
        body,
    ))
    story.append(Spacer(1, 0.1 * inch))

    # Key terms table
    money = lambda v: f"${v:,.0f}"
    pct = lambda v: f"{v:.3f}%".rstrip("0").rstrip(".") + "%"
    terms_data = [
        ["Maximum Purchase Price",  f"{money(max_pp_low)} – {money(max_pp_high)}"],
        ["Minimum Down Payment",    f"{down_pct_low:.0f}%"],
        ["Interest Rate Range",     f"{rate_low_pct:.3f}% – {rate_high_pct:.3f}%"],
    ]
    tbl = Table(terms_data, colWidths=[2.4 * inch, 3.2 * inch], hAlign="LEFT")
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#222")),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEABOVE", (0, 0), (-1, 0), 0.75, colors.HexColor("#888")),
        ("LINEBELOW", (0, -1), (-1, -1), 0.75, colors.HexColor("#888")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 0.2 * inch))

    # Conditions
    story.append(Paragraph("<b>This pre-qualification is subject to:</b>", body))
    for c in [
        "Satisfactory title report on the subject property",
        "Appraisal supporting the purchase price and projected rental income",
        "Final loan-program availability at the time of application",
    ]:
        story.append(Paragraph(f"• {c}", body))
    story.append(Spacer(1, 0.15 * inch))

    # Disclaimers
    story.append(Paragraph("<b>Important Disclosures</b>", head))
    story.append(Paragraph(
        "This Pre-Qualification is not a commitment to lend. Any material change "
        "in your financial or employment status will require re-qualification. "
        "Any material omission or misrepresentation in your loan application may "
        "void this Pre-Qualification.",
        body,
    ))
    story.append(Paragraph(
        f"This approval is valid for {LETTER_VALIDITY_DAYS} days from the date of "
        f"this letter (through {expires_at.strftime('%m/%d/%Y')}). After expiration, "
        "credit documentation must be resubmitted to extend the pre-qualification.",
        body,
    ))
    story.append(Spacer(1, 0.25 * inch))

    # Sign-off
    story.append(Paragraph("Please contact me with any questions.", body))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph("Sincerely,", body))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(f"<b>{signer['lo_name']}</b>", body))
    story.append(Paragraph(signer["lo_title"], body))
    story.append(Paragraph(signer["lo_email"], body))
    story.append(Paragraph(signer["lo_phone"], body))

    doc.build(story)
    return buf.getvalue()


# ── Audit + send ─────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PrequalLetter:
    letter_id: str
    prequal_id: str
    application_id: str
    borrower_name: str
    borrower_email: str
    max_pp_low: float
    max_pp_high: float
    rate_low_pct: float
    rate_high_pct: float
    down_pct_low: float
    liquid_assets: float
    monthly_rent_used: Optional[float]
    pdf_base64: str
    sent_to: str
    zap_fired: bool
    expires_at: str
    issued_at: str
    breakdown: dict[str, Any]


def _load_prequal(prequal_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = fetchone(
            conn,
            "SELECT id, user_id, borrower_data, property_data, suggested_product, "
            "dscr, ltv, score FROM loan_prequals WHERE id = ?",
            (prequal_id,),
        )
    if not row:
        return None
    out = dict(row)
    for k in ("borrower_data", "property_data"):
        raw = out.get(k) or "{}"
        if isinstance(raw, str):
            try:
                out[k] = json.loads(raw)
            except json.JSONDecodeError:
                out[k] = {}
    return out


def _find_application_for_prequal(prequal_id: str) -> Optional[str]:
    with get_conn() as conn:
        row = fetchone(
            conn,
            "SELECT id FROM loan_applications WHERE prequal_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (prequal_id,),
        )
    return row["id"] if row else None


def generate_and_send(
    prequal_id: str,
    *,
    liquid_assets_override: Optional[float] = None,
    monthly_rent_override: Optional[float] = None,
    skip_send: bool = False,
) -> PrequalLetter:
    """
    Top-level pipeline. Reads the prequal, sums bank-stmt balances (or accepts
    an override for tests / LO manual control), computes the max-PP range,
    renders the PDF, fires the Zapier hook, writes the audit row.

    Raises ValueError if the prequal doesn't exist.
    """
    prequal = _load_prequal(prequal_id)
    if not prequal:
        raise ValueError(f"prequal not found: {prequal_id!r}")

    borrower = prequal.get("borrower_data", {}) or {}
    prop = prequal.get("property_data", {}) or {}
    borrower_name = borrower.get("name") or "Borrower"
    borrower_email = borrower.get("email") or ""

    # Liquidity: prefer the OCR-summed value; fall back to the prequal's
    # self-reported figure, and accept an override for LO manual adjustments.
    if liquid_assets_override is not None:
        liquid_assets = float(liquid_assets_override)
        intake = {"liquid_assets": liquid_assets, "breakdown": [], "num_bank_stmts_used": 0,
                  "num_bank_stmts_skipped": 0, "source": "override"}
    else:
        application_id = _find_application_for_prequal(prequal_id) or ""
        intake = extract_liquidity_from_intake(application_id) if application_id else {
            "liquid_assets": 0.0, "breakdown": [], "num_bank_stmts_used": 0,
            "num_bank_stmts_skipped": 0,
        }
        intake["source"] = "intake_documents"
        if intake["liquid_assets"] <= 0:
            # No OCR'd bank stmts → fall back to borrower self-reported liquidity.
            intake["liquid_assets"] = float(borrower.get("liquidity") or 0.0)
            intake["source"] = "borrower_self_reported"
        liquid_assets = float(intake["liquid_assets"])

    monthly_rent = monthly_rent_override if monthly_rent_override is not None else prop.get("monthly_rent") or 0.0
    monthly_rent = float(monthly_rent or 0.0)

    rng = compute_max_pp_range(
        liquid_assets=liquid_assets,
        monthly_rent=monthly_rent if monthly_rent > 0 else None,
    )

    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(days=LETTER_VALIDITY_DAYS)

    pdf_bytes = render_letter_pdf(
        borrower_name=borrower_name,
        borrower_email=borrower_email,
        max_pp_low=rng["max_pp_low"],
        max_pp_high=rng["max_pp_high"],
        issued_at=issued_at,
    )
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

    letter_id = f"pql_{uuid.uuid4().hex[:14]}"
    application_id = _find_application_for_prequal(prequal_id) or ""

    payload = {
        "letter_id": letter_id,
        "prequal_id": prequal_id,
        "application_id": application_id,
        "borrower_name": borrower_name,
        "borrower_email": borrower_email,
        "max_pp_low": rng["max_pp_low"],
        "max_pp_high": rng["max_pp_high"],
        "rate_low_pct": 5.875,
        "rate_high_pct": 8.0,
        "down_pct_low": 20.0,
        "liquid_assets": liquid_assets,
        "monthly_rent_used": monthly_rent if monthly_rent > 0 else None,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "pdf_base64": pdf_b64,
    }

    zap_fired = False
    if not skip_send and borrower_email:
        result = fire_zap("prequal_letter_sent", payload, correlation_id=letter_id)
        zap_fired = bool(result.get("success"))

    breakdown = {
        "intake": intake,
        "range": rng,
    }

    with get_conn() as conn:
        insert(conn, "prequal_letters", {
            "letter_id": letter_id,
            "prequal_id": prequal_id,
            "application_id": application_id,
            "borrower_name": borrower_name,
            "borrower_email": borrower_email,
            "max_pp_low": rng["max_pp_low"],
            "max_pp_high": rng["max_pp_high"],
            "liquid_assets": liquid_assets,
            "monthly_rent_used": monthly_rent if monthly_rent > 0 else None,
            "rate_low_pct": 5.875,
            "rate_high_pct": 8.0,
            "down_pct_low": 20.0,
            "issued_at": issued_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "zap_fired": 1 if zap_fired else 0,
            "sent_to": borrower_email if zap_fired else "",
            "breakdown": json.dumps(breakdown),
            "created_at": _now(),
        })

    return PrequalLetter(
        letter_id=letter_id,
        prequal_id=prequal_id,
        application_id=application_id,
        borrower_name=borrower_name,
        borrower_email=borrower_email,
        max_pp_low=rng["max_pp_low"],
        max_pp_high=rng["max_pp_high"],
        rate_low_pct=5.875,
        rate_high_pct=8.0,
        down_pct_low=20.0,
        liquid_assets=liquid_assets,
        monthly_rent_used=monthly_rent if monthly_rent > 0 else None,
        pdf_base64=pdf_b64,
        sent_to=borrower_email if zap_fired else "",
        zap_fired=zap_fired,
        expires_at=expires_at.isoformat(),
        issued_at=issued_at.isoformat(),
        breakdown=breakdown,
    )


def get_letter(letter_id: str) -> Optional[dict[str, Any]]:
    """Audit-row read; PDF bytes are not persisted, so this returns metadata only."""
    with get_conn() as conn:
        row = fetchone(
            conn, "SELECT * FROM prequal_letters WHERE letter_id = ?", (letter_id,),
        )
    if not row:
        return None
    out = dict(row)
    raw = out.get("breakdown") or "{}"
    if isinstance(raw, str):
        try:
            out["breakdown"] = json.loads(raw)
        except json.JSONDecodeError:
            out["breakdown"] = {}
    return out
