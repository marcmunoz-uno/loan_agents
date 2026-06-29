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

TRANCHI_API_SECRET = os.environ.get("TRANCHI_API_SECRET", "dev-secret-change-me")


def require_tranchi_auth(f):
    """Verify requests using Bearer token or X-API-Key header.

    Uses hmac.compare_digest for constant-time comparison so the secret can't
    be recovered via a timing side-channel.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        secret = TRANCHI_API_SECRET

        auth_header = request.headers.get("Authorization", "")
        if hmac.compare_digest(auth_header, f"Bearer {secret}"):
            return f(*args, **kwargs)

        api_key = request.headers.get("X-API-Key", "")
        if hmac.compare_digest(api_key, secret):
            return f(*args, **kwargs)

        return jsonify({"error": "Unauthorized"}), 401

    return decorated
