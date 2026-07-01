"""
shared/config.py — runtime environment detection + startup config validation.

`is_production()` decides whether the service runs in "fail-closed" mode
(reject webhooks with no secret, refuse the default API secret, hide error
detail). It is intentionally conservative: anything Render-hosted is treated as
production unless explicitly told otherwise via LOAN_AGENTS_ENV.
"""

from __future__ import annotations

import os


def is_production() -> bool:
    """True in production. Render injects RENDER=true; LOAN_AGENTS_ENV overrides.

    (The fatal default-secret check lives in shared.auth.assert_secret_configured,
    which uses its own APP_ENV/pytest gating.)
    """
    env = os.environ.get("LOAN_AGENTS_ENV", "").strip().lower()
    if env in ("production", "prod"):
        return True
    if env in ("dev", "development", "test", "local"):
        return False
    return bool(os.environ.get("RENDER"))


def startup_warnings() -> list[str]:
    """Non-fatal config gaps worth logging at boot (features degrade, but the
    service still starts). Kept separate so a missing API key never bricks a
    deploy — LLM calls already fail loudly on first use."""
    warnings: list[str] = []
    if is_production() and not os.environ.get("ANTHROPIC_API_KEY"):
        warnings.append("ANTHROPIC_API_KEY is not set — chat + vision OCR will fail on use")
    return warnings
