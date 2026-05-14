"""
loan_officer/workflows.py — Loan application state machine.

States:
    NEW → PREQUAL_PENDING → PREQUAL_SCORED → APP_STARTED → APP_DOCS_PENDING
        → APP_SUBMITTED → UNDERWRITING → APPROVED | DECLINED | CONDITIONS
        → CLOSING → FUNDED

Transitions are validated; invalid transitions raise ValueError.
All state changes are appended to the application's audit_log.
"""

from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Any, Optional


# ── State definitions ─────────────────────────────────────────────────────────

STATES = [
    "NEW",
    "PREQUAL_PENDING",
    "PREQUAL_SCORED",
    "APP_STARTED",
    "APP_DOCS_PENDING",
    "APP_SUBMITTED",
    "UNDERWRITING",
    "APPROVED",
    "CONDITIONS",
    "DECLINED",
    "CLOSING",
    "FUNDED",
]

TERMINAL_STATES = {"APPROVED", "DECLINED", "FUNDED"}

# Valid transitions: state → set of next allowed states
TRANSITIONS: dict[str, set[str]] = {
    "NEW":             {"PREQUAL_PENDING", "PREQUAL_SCORED"},
    "PREQUAL_PENDING": {"PREQUAL_SCORED", "DECLINED"},
    "PREQUAL_SCORED":  {"APP_STARTED", "DECLINED"},
    "APP_STARTED":     {"APP_DOCS_PENDING", "APP_SUBMITTED", "DECLINED"},
    "APP_DOCS_PENDING":{"APP_SUBMITTED", "APP_STARTED", "DECLINED"},
    "APP_SUBMITTED":   {"UNDERWRITING", "APP_DOCS_PENDING", "DECLINED"},
    "UNDERWRITING":    {"APPROVED", "CONDITIONS", "DECLINED"},
    "APPROVED":        {"CLOSING"},
    "CONDITIONS":      {"UNDERWRITING", "APPROVED", "DECLINED"},
    "CLOSING":         {"FUNDED", "DECLINED"},
    "DECLINED":        set(),  # terminal
    "FUNDED":          set(),  # terminal
}

# Actions to fire when entering a state (stub — extend with real side effects)
STATE_ENTRY_ACTIONS: dict[str, str] = {
    "PREQUAL_PENDING": "Run pre-qualification scoring",
    "PREQUAL_SCORED":  "Notify borrower of prequal result via Tranchi outbound agent",
    "APP_DOCS_PENDING":"Send document checklist to borrower",
    "APP_SUBMITTED":   "Submit application to selected lender partner",
    "UNDERWRITING":    "Await lender underwriting decision",
    "APPROVED":        "Notify borrower of approval — trigger celebration message",
    "CONDITIONS":      "Send conditions list to borrower — request resolution",
    "DECLINED":        "Notify borrower of decline — explain reasons and alternatives",
    "CLOSING":         "Coordinate closing — title, wire instructions, final walkthrough",
    "FUNDED":          "Confirm funding — update Tranchi deal status to FUNDED",
}


# ── State machine functions ───────────────────────────────────────────────────

def validate_transition(current_state: str, new_state: str) -> None:
    """Raise ValueError if the transition is not allowed."""
    if current_state not in TRANSITIONS:
        raise ValueError(f"Unknown state: {current_state}")
    if new_state not in STATES:
        raise ValueError(f"Unknown target state: {new_state}")
    allowed = TRANSITIONS.get(current_state, set())
    if new_state not in allowed:
        raise ValueError(
            f"Invalid transition: {current_state} → {new_state}. "
            f"Allowed: {sorted(allowed) or 'none (terminal state)'}"
        )


def transition(
    current_state: str,
    new_state: str,
    audit_log: list[dict],
    actor: str = "system",
    payload: Optional[dict[str, Any]] = None,
) -> tuple[str, list[dict]]:
    """
    Validate and apply a state transition.

    Returns the new state and updated audit_log.
    Does NOT write to the database — caller is responsible for persisting.
    """
    validate_transition(current_state, new_state)

    event = {
        "event_type": "state_transition",
        "from_state": current_state,
        "to_state": new_state,
        "actor": actor,
        "action": STATE_ENTRY_ACTIONS.get(new_state, ""),
        "payload": payload or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    updated_log = audit_log + [event]
    return new_state, updated_log


def add_audit_event(
    audit_log: list[dict],
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    actor: str = "system",
) -> list[dict]:
    """Append a non-transition audit event."""
    event = {
        "event_type": event_type,
        "actor": actor,
        "payload": payload or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    return audit_log + [event]


def state_summary(state: str) -> dict[str, Any]:
    """Return human-readable info about a state."""
    return {
        "state": state,
        "is_terminal": state in TERMINAL_STATES,
        "allowed_next": sorted(TRANSITIONS.get(state, set())),
        "entry_action": STATE_ENTRY_ACTIONS.get(state, ""),
    }


def parse_audit_log(raw: str | list) -> list[dict]:
    """Parse audit_log from JSON string or list."""
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
