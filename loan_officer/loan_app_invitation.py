"""
loan_officer/loan_app_invitation.py — Email the borrower their 1003 registration link.

When the borrower goes under contract (PSA classified), the next step is for
them to complete the full loan application (URLA Form 1003). For the Munoz &
Co. setup, the borrower registers + fills it out at
    https://2589631.my1003app.com/0/register

We email them that link automatically when their PSA arrives, via the same
Zapier MCP → Gmail Send pipeline used for the prequal letter.

Config:
    LOAN_APPLICATION_URL — overridable; defaults to the Munoz firm's POS URL
    LO_SIGNER_*          — firm letterhead + signer block, same env vars used
                            by the prequal letter renderer
    ZAPIER_MCP_*         — same MCP creds used to send the prequal letter
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from shared.zapier_mcp import ZapierMCPClient

DEFAULT_APP_URL = os.environ.get(
    "LOAN_APPLICATION_URL", "https://2589631.my1003app.com/0/register"
)

# Re-use the same signer fields as the prequal letter so the email looks
# consistent with the rest of the borrower's Tranchi correspondence.
SIGNER = {
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


# ── Email rendering ───────────────────────────────────────────────────────────

def render_invitation_html(
    *,
    borrower_name: str,
    property_address: str,
    purchase_price: Optional[float],
    closing_date: str,
    app_url: str = DEFAULT_APP_URL,
) -> str:
    """Short, warm follow-up email body with the 1003 registration link."""
    pp = f"${purchase_price:,.0f}" if purchase_price else "your subject property"
    closing = closing_date or "the agreed closing date"
    return f"""\
<div style="font-family:Georgia,serif;max-width:640px;margin:0 auto;color:#222;line-height:1.55;">
  <p>Hi {borrower_name},</p>

  <p>Great news — we've received your executed purchase contract for
  <b>{property_address or 'the subject property'}</b>{' at ' + pp if purchase_price else ''}.
  Closing is targeted for <b>{closing}</b>.</p>

  <p>The next step is to complete your full loan application (URLA Form 1003).
  Please use the link below to register and fill it out — it takes about
  10–15 minutes:</p>

  <p style="margin:24px 0;">
    <a href="{app_url}" style="display:inline-block;padding:12px 24px;
       background-color:#1a3a5c;color:#fff;text-decoration:none;
       border-radius:4px;font-weight:bold;">
      Complete Your Loan Application
    </a>
  </p>

  <p style="font-size:12px;color:#666;">Or copy this URL into your browser:<br>
  <code>{app_url}</code></p>

  <p>Once you've submitted the application, I'll review it and start your
  file with underwriting. If anything is unclear or you'd prefer a quick
  call to walk through it together, just reply to this email or use the
  number below.</p>

  <p>Sincerely,</p>

  <p><b>{SIGNER['lo_name']}</b><br>
    {SIGNER['lo_title']}<br>
    Email: <a href="mailto:{SIGNER['lo_email']}">{SIGNER['lo_email']}</a><br>
    Mobile: {SIGNER['lo_phone']}
  </p>

  <hr style="border:none;border-top:1px solid #ddd;margin:24px 0 12px;">
  <p style="font-size:11px;color:#666;text-align:center;">
    {SIGNER['firm_short_name']} &nbsp;-&nbsp; {SIGNER['firm_address_line_1']}, {SIGNER['firm_address_line_2']}
    &nbsp;-&nbsp; {SIGNER['firm_domain']}
    &nbsp;-&nbsp; <a href="{SIGNER['firm_cta_url']}">{SIGNER['firm_cta_text']}</a>
  </p>
</div>"""


# ── Send via Zapier MCP → Gmail ───────────────────────────────────────────────

def send_loan_app_invitation(
    *,
    borrower_name: str,
    borrower_email: str,
    property_address: str,
    purchase_price: Optional[float],
    closing_date: str,
    app_url: Optional[str] = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    """
    Fire Gmail Send Email via Zapier MCP with the 1003 registration link.
    Returns {ok, status, sent_to, app_url, error?}.
    """
    if not borrower_email:
        return {"ok": False, "status": "skipped:no_borrower_email", "sent_to": "",
                "app_url": app_url or DEFAULT_APP_URL}

    client = ZapierMCPClient()
    if not client.configured:
        return {"ok": False, "status": "skipped:zapier_mcp_not_configured",
                "sent_to": "", "app_url": app_url or DEFAULT_APP_URL}

    url = app_url or DEFAULT_APP_URL
    body_html = render_invitation_html(
        borrower_name=borrower_name,
        property_address=property_address,
        purchase_price=purchase_price,
        closing_date=closing_date,
        app_url=url,
    )

    params = {
        "to":                  [borrower_email],
        "subject":             "Next step: Complete your loan application",
        "body":                body_html,
        "body_type":           "html",
        "from_name":           SIGNER["lo_name"],
        "signature_delimiter": "false",
    }

    try:
        client.execute(
            app="gmail", action="message", mode="write",
            params=params,
            instructions=(
                "Send the post-PSA follow-up email to the borrower. Body is "
                "fully rendered HTML; do not paraphrase. "
                + (f"correlation_id={correlation_id}" if correlation_id else "")
            ),
            output="Gmail message_id of the sent email.",
        )
    except Exception as e:
        return {
            "ok":      False,
            "status":  f"failed:{type(e).__name__}: {str(e)[:200]}",
            "sent_to": "",
            "app_url": url,
        }

    return {"ok": True, "status": "sent", "sent_to": borrower_email, "app_url": url}
