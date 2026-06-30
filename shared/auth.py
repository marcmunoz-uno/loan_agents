"""
shared/auth.py — Bearer token middleware matching tranchi-outbound-agent pattern.

Usage:
    from shared.auth import require_tranchi_auth

    @app.route("/api/loan/prequal", methods=["POST"])
    @require_tranchi_auth
    def prequal():
        ...

Accepts either:
    Authorization: Bearer <TRANCHI_API_SECRET>
    X-API-Key: <TRANCHI_API_SECRET>
"""

import hmac
import os
from functools import wraps
from flask import request, jsonify

_DEFAULT_SECRET = "dev-secret-change-me"
TRANCHI_API_SECRET = os.environ.get("TRANCHI_API_SECRET", _DEFAULT_SECRET)


def assert_secret_configured() -> None:
    """
    Fail fast at boot if we're running with the default dev secret outside of
    a dev/test environment. A coordinator that texts real counterparties must
    not silently fall back to a publicly-known token. Call from create_app().

    Set APP_ENV=development (or run under pytest) to allow the default.
    """
    app_env = os.environ.get("APP_ENV", "").lower()
    is_dev = app_env in ("dev", "development", "local", "test")
    is_pytest = "PYTEST_CURRENT_TEST" in os.environ
    if TRANCHI_API_SECRET in ("", _DEFAULT_SECRET) and not (is_dev or is_pytest):
        raise RuntimeError(
            "TRANCHI_API_SECRET is unset, empty, or still the default dev value. "
            "Set a real secret in the environment, or set APP_ENV=development "
            "to run locally with the default."
        )


def _matches(candidate: str, expected: str) -> bool:
    """Constant-time comparison to avoid leaking the secret via timing."""
    return hmac.compare_digest(candidate, expected)


def require_tranchi_auth(f):
    """Verify requests using Bearer token or X-API-Key header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = TRANCHI_API_SECRET

        # A blank secret must never authenticate — otherwise an empty
        # credential would match it via constant-time compare.
        if not secret:
            return jsonify({"error": "Unauthorized"}), 401

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and _matches(auth_header.removeprefix("Bearer "), secret):
            return f(*args, **kwargs)

        api_key = request.headers.get("X-API-Key", "")
        if api_key and _matches(api_key, secret):
            return f(*args, **kwargs)

        return jsonify({"error": "Unauthorized"}), 401

    return decorated
