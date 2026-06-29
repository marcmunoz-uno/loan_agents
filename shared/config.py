"""
shared/config.py — runtime environment detection + startup config validation.

`is_production()` decides whether the service runs in "fail-closed" mode
(reject webhooks with no secret, refuse the default API secret, hide error
detail). It is intentionally conservative: anything Render-hosted is treated as
production unless explicitly told otherwise via LOAN_AGENTS_ENV.
"""

from __future__ import annotations

import os

DEFAULT_API_SECRET = "dev-secret-change-me"


def is_production() -> bool:
    """True in production. Render injects RENDER=true; LOAN_AGENTS_ENV overrides."""
    env = os.environ.get("LOAN_AGENTS_ENV", "").strip().lower()
    if env in ("production", "prod"):
        return True
    if env in ("dev", "development", "test", "local"):
        return False
    return bool(os.environ.get("RENDER"))


def validate_startup_config() -> list[str]:
    """
    Return a list of fatal config problems. Empty list = OK.

    Only enforced in production (so dev/test boots with defaults). The caller
    decides whether to raise; this keeps the policy in one place.
    """
    problems: list[str] = []
    if not is_production():
        return problems

    secret = os.environ.get("TRANCHI_API_SECRET", "")
    if not secret:
        problems.append("TRANCHI_API_SECRET is not set")
    elif secret == DEFAULT_API_SECRET:
        problems.append("TRANCHI_API_SECRET is still the insecure default")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append("ANTHROPIC_API_KEY is not set (chat + vision OCR will fail)")

    return problems
