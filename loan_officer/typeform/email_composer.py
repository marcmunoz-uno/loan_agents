"""
loan_officer/typeform/email_composer.py — Render + send the post-intake email.

Matches the visual + send-pipeline of loan_officer.loan_app_invitation and
loan_officer.prequal_letter so every borrower-facing email from the AI Loan
Officer looks identical: Munoz & Co. letterhead, Marc Munoz signer block,
HTML body, sent via Zapier MCP → Gmail with from_name set so it lands as
"Marc Munoz <replies@Munoz.ltd>" in the borrower's inbox.

Body is rendered deterministically (no LLM in the body) so the email is
predictable and reviewable; the soft-prequal status drives which copy block
shows up. The intake form does not collect property data, so the email's
job is to (a) acknowledge intake, (b) report the soft-prequal outcome, and
(c) move the borrower to the next step (property details / fix-up / call).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from shared.zapier_mcp import ZapierMCPClient

logger = logging.getLogger(__name__)


# ── Signer / firm config (mirrors loan_app_invitation + prequal_letter) ─────

SIGNER = {
    "firm_name":           os.environ.get("LO_FIRM_NAME", "MUNOZ & CO. LTD"),
    "firm_short_name":     os.environ.get("LO_FIRM_SHORT_NAME", "Munoz & Co. Ltd"),
    "firm_address_line_1": os.environ.get("LO_FIRM_ADDR_1", "99 Wall Street, Suite 4041"),
    "firm_address_line_2": os.environ.get("LO_FIRM_ADDR_2", "New York, NY, 10005"),
    "firm_domain":         os.environ.get("LO_FIRM_DOMAIN", "Munoz.Ltd"),
    "firm_cta_url":        os.environ.get("LO_FIRM_CTA_URL", "https://munoz.ltd"),
    "firm_cta_text":       os.environ.get("LO_FIRM_CTA_TEXT", "Book a Call"),
    "lo_name":             os.environ.get("LO_SIGNER_NAME", "Marc Munoz"),
    "lo_title":            os.environ.get("LO_SIGNER_TITLE", "Senior Loan Officer"),
    "lo_email":            os.environ.get("LO_SIGNER_EMAIL", "marc@munoz.ltd"),
    "lo_phone":            os.environ.get("LO_SIGNER_PHONE", "(917) 981-0032"),
}


# ── Subjects + status-driven copy blocks ──────────────────────────────────────

_SUBJECTS = {
    "pass":        "Your Pre-Qualification Intake — Next: Property Details",
    "conditional": "Your Pre-Qualification Intake — A Few Items to Address",
    "decline":     "Your Pre-Qualification Intake — Update on Your Submission",
}

_OPENERS = {
    "pass": (
        "Thank you for completing our pre-qualification intake. Based on the borrower "
        "information and documentation you shared, we are pleased to advise you that "
        "you have been <b>conditionally pre-qualified</b> for a Non-QM DSCR mortgage "
        "loan, pending review of the subject property."
    ),
    "conditional": (
        "Thank you for completing our pre-qualification intake. Based on the borrower "
        "information you shared, you are <b>conditionally pre-qualified</b> for a "
        "Non-QM DSCR mortgage loan. A few items need to be addressed before we can "
        "issue a formal pre-qualification letter."
    ),
    "decline": (
        "Thank you for completing our pre-qualification intake. After reviewing the "
        "information you provided, we are not able to move forward with a Non-QM DSCR "
        "pre-qualification at this time. The details below explain why and outline "
        "concrete next steps."
    ),
}

_NEXT_STEPS_HTML = {
    "pass": (
        "<p>To finalize and issue your formal Pre-Qualification Letter, we need a few "
        "details about the property you intend to purchase. Please reply to this "
        "email with:</p>"
        "<ul>"
        "  <li>Property address</li>"
        "  <li>Purchase price</li>"
        "  <li>Expected monthly rent (actual lease or market estimate)</li>"
        "  <li>Annual property taxes</li>"
        "  <li>Annual insurance estimate</li>"
        "</ul>"
        "<p>Once we have those, we will run the full DSCR fit-score and issue your "
        "pre-qualification letter the same business day.</p>"
    ),
    "conditional": (
        "<p>Please address the items above and reply to this email when they're "
        "ready. If anything is unclear or you'd prefer a quick call to walk through "
        "it together, just reply or use the number below.</p>"
    ),
    "decline": (
        "<p>If you would like to discuss options — credit repair timelines, "
        "alternative loan programs, or a co-borrower scenario — please reply to "
        "this email or book a call using the link in the footer.</p>"
    ),
}


# ── HTML rendering ────────────────────────────────────────────────────────────

def render_intake_email_html(
    *,
    borrower_name: str,
    soft_prequal_status: str,
    decision_reasons: list[str],
    missing_required_docs: list[str],
    issued_at: Optional[datetime] = None,
    signer: Optional[dict[str, str]] = None,
) -> str:
    """Render the post-intake email body. Mirrors the prequal letter visual."""
    sig = {**SIGNER, **(signer or {})}
    issued = issued_at or datetime.now(timezone.utc)
    status = (soft_prequal_status or "conditional").lower()
    if status not in _OPENERS:
        status = "conditional"

    opener = _OPENERS[status]
    next_steps_html = _NEXT_STEPS_HTML[status]

    reasons_html = ""
    if decision_reasons:
        items = "".join(f"<li>{r}</li>" for r in decision_reasons)
        reasons_html = f"<p>Notes from our review:</p><ul>{items}</ul>"

    missing_html = ""
    if missing_required_docs:
        items = "".join(f"<li>{m}</li>" for m in missing_required_docs)
        missing_html = (
            "<p>Required documents we still need from you:</p>"
            f"<ul>{items}</ul>"
        )

    return f"""\
<div style="font-family:Georgia,serif;max-width:640px;margin:0 auto;color:#222;line-height:1.55;">
  <div style="text-align:center;margin-bottom:32px;">
    <div style="font-size:16px;font-weight:bold;">{sig['firm_name']}</div>
    <div style="font-size:14px;font-weight:bold;">PRE-QUALIFICATION INTAKE — NEXT STEPS</div>
  </div>

  <p><b>DATE:</b> {issued.strftime('%m/%d/%Y')}</p>
  <p><b>TO:</b> {borrower_name}</p>
  <p><b>RE:</b> NON-QM DSCR PRE-QUALIFICATION INTAKE</p>

  <p>{opener}</p>

  {reasons_html}
  {missing_html}

  {next_steps_html}

  <p>It is important to note that, should your financial, employment, or credit
  standing change, this assessment will be subject to re-qualifying and
  verification. Any material omission or misrepresentation in your submission
  may void this assessment. This is not a commitment to lend.</p>

  <p>Sincerely,</p>

  <p>{sig['lo_name']}</p>

  <p style="margin-bottom:24px;">
    {sig['lo_title']}<br>
    Email: <a href="mailto:{sig['lo_email']}">{sig['lo_email']}</a><br>
    Mobile: {sig['lo_phone']}
  </p>

  <hr style="border:none;border-top:1px solid #ddd;margin:24px 0 12px;">
  <p style="font-size:11px;color:#666;text-align:center;">
    {sig['firm_short_name']} &nbsp;-&nbsp; {sig['firm_address_line_1']}, {sig['firm_address_line_2']}
    &nbsp;-&nbsp; {sig['firm_domain']}
    &nbsp;-&nbsp; <a href="{sig['firm_cta_url']}">{sig['firm_cta_text']}</a>
  </p>
</div>"""


def compose_email(intake: dict, soft_prequal: dict) -> dict:
    """
    Returns {"subject": ..., "body": ...} for the post-intake email.

    `intake` is the mapped row (see mapper.map_payload).
    `soft_prequal` is SoftPrequalResult.__dict__.
    """
    first_name = intake.get("first_name") or "Borrower"
    last_name  = intake.get("last_name") or ""
    borrower_name = f"{first_name} {last_name}".strip()
    status = (soft_prequal.get("status") or "conditional").lower()
    if status not in _SUBJECTS:
        status = "conditional"

    body = render_intake_email_html(
        borrower_name=borrower_name,
        soft_prequal_status=status,
        decision_reasons=list(soft_prequal.get("decision_reasons", []) or []),
        missing_required_docs=list(soft_prequal.get("missing_required_docs", []) or []),
    )
    return {"subject": _SUBJECTS[status], "body": body}


# ── Send via Zapier MCP → Gmail (matches loan_app_invitation params) ─────────

def send_email(to: str, subject: str, body: str, correlation_id: str = "") -> dict:
    """
    Send via Zapier MCP `gmail.message:write` using the same params shape as
    loan_app_invitation + prequal_letter, so the email is sent from
    replies@Munoz.ltd with from_name="Marc Munoz" and the Gmail
    auto-signature suppressed.
    """
    if not to:
        return {"ok": False, "error": "No recipient email on intake",
                "status": "skipped:no_borrower_email"}

    client = ZapierMCPClient()
    if not client.configured:
        return {"ok": False, "skipped": True,
                "status": "skipped:zapier_mcp_not_configured",
                "error": "Zapier MCP not configured"}

    params: dict[str, Any] = {
        "to":                  [to],
        "subject":             subject,
        "body":                body,
        "body_type":           "html",
        "from_name":           SIGNER["lo_name"],
        "signature_delimiter": "false",
    }

    try:
        client.execute(
            app="gmail",
            action="message",
            mode="write",
            params=params,
            instructions=(
                "Send the post-intake pre-qualification email to the borrower. "
                "Body is fully rendered HTML; do not paraphrase. "
                + (f"correlation_id={correlation_id}" if correlation_id else "")
            ),
            output="Return the Gmail message id and thread id.",
        )
    except ModuleNotFoundError:
        return {"ok": False, "error": "Zapier MCP module missing",
                "status": "skipped:zapier_mcp_module_missing", "skipped": True}
    except Exception as exc:
        logger.error("[typeform.email_composer] Zapier MCP send failed: %s", exc)
        return {"ok": False, "error": str(exc), "status": f"failed:{type(exc).__name__}"}

    return {
        "ok": True,
        "status": "sent",
        "sent_to": to,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
