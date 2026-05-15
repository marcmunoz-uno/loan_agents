"""
loan_officer/intake/chatbot.py — Stateful intake chat session for the Tranchi - Loan Officer.

A ChatSession tracks the full conversation history and the current deal context
(borrower data points collected so far). Each call to `handle_message` sends the
conversation to the LLM with a targeted intake system prompt and returns a
structured reply that may include:
  - a follow-up question for the borrower
  - an updated deal_context dict
  - a flag `intake_complete` when all 6 key data points are collected

Usage:
    session = ChatSession(session_id="sess_abc123", deal_id="deal_xyz")
    result = session.handle_message("I have a duplex bringing in $2,800/month rent")
    # result["reply"]           → assistant message text
    # result["deal_context"]    → updated partial borrower/property profile
    # result["intake_complete"] → True when ready to hand off to prequal
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.llm import chat
from shared.db import get_conn, fetchone, insert, update

# ── System prompt for the intake chatbot ─────────────────────────────────────

_INTAKE_SYSTEM_PROMPT = """
You are the Tranchi - Loan Officer intake agent at Tranchi.ai. Your single job is
to collect the 6 key data points needed to pre-qualify the borrower:

1. Credit score range (e.g. "680–720", "above 740")
2. Monthly or annual gross income (or "no-income DSCR" if investor)
3. Liquid assets / available down payment ($)
4. Subject property address and type (SFR, duplex, multifamily, commercial)
5. Projected or actual monthly rent ($)
6. Investment goal: buy-hold, fix-flip, BRRRR, or primary residence

Ask for ONE missing data point at a time. Be friendly but efficient. When all 6
are collected, output a JSON block at the end of your reply in this format:
{"intake_complete": true, "deal_context": {<collected fields>}}

Until then, ask the next missing question naturally in conversation.
""".strip()


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class DealContext:
    """Partial borrower/property profile accumulated during intake."""
    credit_score_range: Optional[str] = None
    monthly_income: Optional[float] = None
    liquid_assets: Optional[float] = None
    property_address: Optional[str] = None
    property_type: Optional[str] = None
    monthly_rent: Optional[float] = None
    investment_goal: Optional[str] = None  # buy_hold | fix_flip | brrrr | primary

    def missing_fields(self) -> list[str]:
        """Return names of data points not yet collected."""
        checks = {
            "credit_score_range": self.credit_score_range,
            "monthly_income": self.monthly_income,
            "liquid_assets": self.liquid_assets,
            "property_address": self.property_address,
            "monthly_rent": self.monthly_rent,
            "investment_goal": self.investment_goal,
        }
        return [k for k, v in checks.items() if v is None]

    def is_complete(self) -> bool:
        return len(self.missing_fields()) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "credit_score_range": self.credit_score_range,
            "monthly_income": self.monthly_income,
            "liquid_assets": self.liquid_assets,
            "property_address": self.property_address,
            "property_type": self.property_type,
            "monthly_rent": self.monthly_rent,
            "investment_goal": self.investment_goal,
        }


@dataclass
class ChatSession:
    """
    Stateful intake chat session.

    Maintains conversation history and accumulated deal context. Persists to
    the `intake_sessions` table (see migration 002). Instantiate with
    `ChatSession.load(session_id)` to resume, or create fresh with constructor.
    """

    session_id: str
    deal_id: Optional[str] = None
    user_id: Optional[str] = None
    history: list[dict[str, str]] = field(default_factory=list)
    deal_context: DealContext = field(default_factory=DealContext)
    intake_complete: bool = False
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # ── Class methods ─────────────────────────────────────────────────────────

    @classmethod
    def new(cls, user_id: str, deal_id: Optional[str] = None) -> "ChatSession":
        """
        Create a new intake session and persist it.

        TODO: INSERT into intake_sessions table.
        """
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        raise NotImplementedError(
            "TODO: insert into intake_sessions and return ChatSession instance"
        )

    @classmethod
    def load(cls, session_id: str) -> "ChatSession":
        """
        Load an existing session from the database.

        TODO: SELECT from intake_sessions, deserialize history + deal_context JSON.
        """
        raise NotImplementedError(
            "TODO: load session row and reconstruct ChatSession"
        )

    # ── Core method ───────────────────────────────────────────────────────────

    def handle_message(self, user_message: str) -> dict[str, Any]:
        """
        Process one user message and return a structured reply.

        Steps:
          1. Append user message to history.
          2. Build context string from current deal_context.
          3. Call shared.llm.chat with intake system prompt + full history.
          4. Parse LLM reply for any JSON `{intake_complete, deal_context}` block.
          5. Merge extracted fields into self.deal_context.
          6. Persist updated session to DB.
          7. Return structured result dict.

        Returns:
            {
                "reply": str,                 # assistant text to send back
                "deal_context": dict,         # current accumulated context
                "intake_complete": bool,
                "missing_fields": list[str],
            }

        TODO: implement parsing + persistence.
        """
        raise NotImplementedError(
            "TODO: build messages list, call chat(), parse JSON block from reply, "
            "update self.deal_context, persist, return result dict"
        )

    # ── Persistence helpers ───────────────────────────────────────────────────

    def _save(self) -> None:
        """
        Persist current session state to intake_sessions table.

        TODO: upsert session row — history + deal_context as JSON blobs.
        """
        raise NotImplementedError("TODO: upsert to intake_sessions table")

    def _parse_llm_json_block(self, reply_text: str) -> dict[str, Any]:
        """
        Extract and parse the trailing JSON block from an LLM reply if present.

        The LLM is instructed to append:
            {"intake_complete": true, "deal_context": {...}}
        when all data points have been collected.

        Returns the parsed dict or {} if no block found.

        TODO: use regex or split on '{' to find and parse the JSON block safely.
        """
        raise NotImplementedError(
            "TODO: regex-extract JSON block from reply_text and json.loads() it"
        )
