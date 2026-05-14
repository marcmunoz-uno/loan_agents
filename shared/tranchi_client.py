"""
shared/tranchi_client.py — HTTP client for calling the tranchi-outbound-agent service.

Used by both agents to trigger voice calls and iMessage outreach when a deal
milestone warrants direct contact (e.g., loan approval → trigger celebration
message, inspection concern → trigger follow-up call).

Usage:
    from shared.tranchi_client import OutboundClient

    client = OutboundClient()
    client.send_imessage(
        user_id="usr_123",
        phone="+15551234567",
        mode="warm_nurture",
        context="Loan approved — let borrower know next steps."
    )
"""

from __future__ import annotations

import os
import requests
from typing import Any, Optional

OUTBOUND_AGENT_URL = os.environ.get(
    "OUTBOUND_AGENT_URL", "https://tranchi-outbound-agent.onrender.com"
)
TRANCHI_API_SECRET = os.environ.get("TRANCHI_API_SECRET", "dev-secret-change-me")


class OutboundClient:
    """Thin wrapper around the tranchi-outbound-agent REST API."""

    def __init__(self, base_url: str = OUTBOUND_AGENT_URL, secret: str = TRANCHI_API_SECRET):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any], timeout: int = 15) -> dict:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(url, json=payload, headers=self.headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"[OutboundClient] POST {path} failed: {e}")
            return {"error": str(e), "ok": False}

    def health(self) -> dict:
        """Check if the outbound agent is reachable."""
        try:
            resp = requests.get(
                f"{self.base_url}/health", headers=self.headers, timeout=10
            )
            return resp.json()
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}

    def trigger_nurture(
        self, *, user_id: str, phone: str, context: str
    ) -> dict:
        """Trigger warm nurture outreach via iMessage/SMS."""
        return self._post(
            "/api/outreach/nurture",
            {"user_id": user_id, "phone": phone, "context": context},
        )

    def trigger_cold_outreach(
        self, *, user_id: str, phone: str, owner_name: str,
        property_address: str, **kwargs
    ) -> dict:
        """Trigger initial cold outreach to a property owner."""
        payload = {
            "user_id": user_id,
            "phone": phone,
            "owner_name": owner_name,
            "property_address": property_address,
            **kwargs,
        }
        return self._post("/api/outreach/cold", payload)

    def trigger_voice_call(
        self, *, user_id: str, phone: str, owner_name: str = "",
        property_address: str = "", **kwargs
    ) -> dict:
        """Initiate an AI voice call to a contact."""
        payload = {
            "user_id": user_id,
            "phone": phone,
            "owner_name": owner_name,
            "property_address": property_address,
            **kwargs,
        }
        return self._post("/api/outreach/call", payload)

    def simulate(self, *, user_id: str, text: str, phone: str = "+10000000001",
                 mode: Optional[str] = None) -> dict:
        """Test the outbound agent without actually sending."""
        payload = {"user_id": user_id, "phone": phone, "text": text}
        if mode:
            payload["mode"] = mode
        return self._post("/api/simulate", payload)
