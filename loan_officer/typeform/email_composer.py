"""
loan_officer/typeform/email_composer.py — AI Loan Officer drafts the
post-intake prequal email and sends it via Zapier MCP gmail action.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.llm import chat
from shared.zapier_mcp import ZapierMCPClient

logger = logging.getLogger(__name__)

_LO_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[1] / "system_prompt.md"

_COMPOSE_INSTRUCTIONS = """\
You are drafting the FIRST email a borrower receives after submitting our
Typeform qualification intake. Compose a personalized soft-prequalification
email. Tone: warm, professional, concise. No marketing fluff.

Reply ONLY with a single JSON object — no markdown fence, no preamble — of the
exact shape:

  {"subject": "<email subject>", "body": "<plain-text email body>"}

The body must:
  - Address the borrower by first name.
  - Briefly state our soft-prequal outcome (pass / conditional / decline).
  - If pass: invite them to share the subject property (address, price,
    rent, taxes, insurance) so we can run a full DSCR fit-score.
  - If conditional: list 2-3 specific items they need to clarify or improve.
  - If decline: be honest about why and suggest concrete next steps
    (credit repair, alternate products).
  - If any required docs are missing, list them clearly.
  - Sign off as "The Tranchi Loan Team".

Keep the body under 220 words. Use line breaks for readability.
"""


def compose_email(intake: dict, soft_prequal: dict) -> dict:
    """
    Generate {"subject": ..., "body": ...} for the borrower's intake email.

    `intake` is the mapped row (mapper.map_payload + system fields).
    `soft_prequal` is SoftPrequalResult.__dict__.
    """
    try:
        system_prompt = _LO_SYSTEM_PROMPT_PATH.read_text()
    except OSError:
        system_prompt = "You are Tranchi's AI Loan Officer for DSCR investment loans."

    context = {
        "borrower": {
            "first_name": intake.get("first_name") or "there",
            "last_name":  intake.get("last_name") or "",
            "email":      intake.get("email", ""),
            "company":    intake.get("company", ""),
            "credit_score_estimate": intake.get("credit_score_estimate"),
            "primary_residence_status": intake.get("primary_residence_status"),
            "spoke_to_loan_officer": intake.get("spoke_to_loan_officer"),
        },
        "soft_prequal": {
            "status": soft_prequal.get("status"),
            "score":  soft_prequal.get("score"),
            "missing_required_docs": soft_prequal.get("missing_required_docs", []),
            "decision_reasons": soft_prequal.get("decision_reasons", []),
        },
    }

    user_msg = (
        _COMPOSE_INSTRUCTIONS
        + "\n\nIntake context (JSON):\n"
        + json.dumps(context, indent=2)
    )

    raw = chat(
        messages=[{"role": "user", "content": user_msg}],
        system=system_prompt,
        model_tier="standard",
        max_tokens=900,
        temperature=0.4,
    )

    return _parse_email_json(raw, intake, soft_prequal)


def _parse_email_json(raw: str, intake: dict, soft_prequal: dict) -> dict:
    """Parse the LLM's JSON; on any failure, fall back to a deterministic template."""
    try:
        # Strip code-fence if the model adds one despite instructions.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "subject" in obj and "body" in obj:
            return {"subject": str(obj["subject"]), "body": str(obj["body"])}
    except Exception as e:
        logger.warning("[typeform.email_composer] LLM JSON parse failed (%s) — falling back to template", e)
    return _fallback_email(intake, soft_prequal)


def _fallback_email(intake: dict, soft_prequal: dict) -> dict:
    """Deterministic email used when the LLM is unavailable or returns garbage."""
    name = intake.get("first_name") or "there"
    status = (soft_prequal.get("status") or "pending").lower()
    score = soft_prequal.get("score")
    missing = soft_prequal.get("missing_required_docs", [])
    reasons = soft_prequal.get("decision_reasons", [])

    if status == "pass":
        opener = (
            f"Thanks for completing our qualification intake. Based on what you shared, "
            f"you're soft-qualified for DSCR financing (score {score}/100). "
            f"To run a full fit-score, please reply with the subject property's address, "
            f"purchase price, monthly rent, annual taxes, and annual insurance."
        )
    elif status == "conditional":
        opener = (
            f"Thanks for completing our qualification intake. You're conditionally "
            f"qualified for DSCR financing (score {score}/100). A few items need attention "
            f"before we can run a full fit-score."
        )
    else:
        opener = (
            f"Thanks for completing our qualification intake. Based on what you shared, "
            f"we aren't able to move forward with DSCR financing right now."
        )

    parts = [f"Hi {name},", "", opener]
    if reasons:
        parts += ["", "Notes from our review:"]
        parts += [f"  - {r}" for r in reasons]
    if missing:
        parts += ["", "Required docs still outstanding:"]
        parts += [f"  - {m}" for m in missing]
    parts += [
        "",
        "Reply to this email and we'll take it from here.",
        "",
        "— The Tranchi Loan Team",
    ]
    body = "\n".join(parts)

    subject_map = {
        "pass":        "You're soft-qualified — next step: property details",
        "conditional": "Soft-qualification: a few items to address",
        "decline":     "Update on your Tranchi qualification intake",
    }
    return {"subject": subject_map.get(status, "Update on your Tranchi qualification intake"), "body": body}


# ── Send via Zapier MCP gmail action ─────────────────────────────────────────

def send_email(to: str, subject: str, body: str) -> dict:
    """
    Send via Zapier MCP `gmail.message:write` — same handler the
    loan_officer persona's `send_borrower_email` tool uses.

    Returns:
        {"ok": True,  "result": <zapier response>}
        {"ok": False, "error": "...", "skipped": True}   when MCP unconfigured
        {"ok": False, "error": "..."}                    on transport error
    """
    if not to:
        return {"ok": False, "error": "No recipient email on intake"}

    client = ZapierMCPClient()
    if not client.configured:
        logger.info("[typeform.email_composer] Zapier MCP not configured — skipping email send to %s", to)
        return {"ok": False, "skipped": True, "error": "Zapier MCP not configured"}

    try:
        result = client.execute(
            app="gmail",
            action="message",
            mode="write",
            params={"to": to, "subject": subject, "body": body},
            instructions=(
                "Send a borrower-intake follow-up email from the Tranchi loan-officer "
                "inbox. Recipient is the prospective borrower who just completed the "
                "Typeform qualification form."
            ),
            output="Return the Gmail message id and thread id.",
        )
        return {"ok": True, "result": result, "sent_at": datetime.now(timezone.utc).isoformat()}
    except Exception as exc:
        logger.error("[typeform.email_composer] Zapier MCP send failed: %s", exc)
        return {"ok": False, "error": str(exc)}
