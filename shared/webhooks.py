"""
shared/webhooks.py — HMAC SHA256 webhook signing and verification.

Matches the pattern used in tranchi-outbound-agent for Blooio/ElevenLabs.

Usage (signing an outbound payload):
    from shared.webhooks import sign_payload
    sig = sign_payload(json.dumps(data), secret)

Usage (verifying an inbound webhook):
    from shared.webhooks import verify_webhook
    @app.route("/api/loan/webhook/lender-update", methods=["POST"])
    @verify_webhook(secret_env="LENDER_WEBHOOK_SECRET")
    def lender_update():
        ...
"""

from __future__ import annotations

import hmac
import hashlib
import os
from functools import wraps
from typing import Union
from flask import request, jsonify


def sign_payload(body: Union[str, bytes], secret: str) -> str:
    """Return hex-encoded HMAC-SHA256 signature for the given body."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Return True if signature matches expected HMAC-SHA256 of body."""
    if not secret:
        return True  # no secret configured → skip in dev
    expected = sign_payload(body, secret)
    return hmac.compare_digest(expected, signature)


def verify_webhook(secret_env: str = "LENDER_WEBHOOK_SECRET",
                   header_name: str = "X-Webhook-Signature"):
    """
    Decorator factory. Verifies the incoming request's HMAC signature.

    Args:
        secret_env: environment variable name holding the shared secret.
        header_name: HTTP header carrying the signature.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            secret = os.environ.get(secret_env, "")
            if not secret:
                # No secret configured — pass through in dev
                return f(*args, **kwargs)
            body = request.get_data()
            sig = request.headers.get(header_name, "")
            if not verify_signature(body, sig, secret):
                return jsonify({"error": "Invalid webhook signature"}), 401
            return f(*args, **kwargs)
        return decorated
    return decorator
