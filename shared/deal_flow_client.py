"""
shared/deal_flow_client.py — HTTP client for the tranchi-deal-flow-agents service.

Mirror of how deal-flow's `shared/loan_agents_client.py` points at us; this
client points the other way. Used when an autonomous flow on loan_agents
needs to wake up TX coordination — most notably, opening a transaction
file the moment an executed PSA lands and gets classified.

Configure with `DEAL_FLOW_URL` (defaults to https://tranchi-deal-flow-agents.onrender.com).
Auth uses the shared `TRANCHI_API_SECRET`.

All methods return the same dict shape:
    {
        "ok":     bool,
        "data":   dict | str | None,   # parsed JSON when available
        "status": int | None,          # HTTP status code (None on transport error)
        "error":  str,                 # only present when ok=False
    }
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

DEAL_FLOW_URL = os.environ.get(
    "DEAL_FLOW_URL", "https://tranchi-deal-flow-agents.onrender.com"
)
TRANCHI_API_SECRET = os.environ.get("TRANCHI_API_SECRET", "dev-secret-change-me")


class DealFlowClient:
    """Thin HTTP wrapper around the tranchi-deal-flow-agents service."""

    def __init__(
        self,
        base_url: str = DEAL_FLOW_URL,
        secret: str = TRANCHI_API_SECRET,
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }

    # ── Health ────────────────────────────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        try:
            resp = requests.get(
                f"{self.base_url}/health",
                headers=self.headers,
                timeout=min(self.timeout, 10),
            )
            return {
                "ok": resp.ok,
                "status": resp.status_code,
                "data": _safe_json(resp),
            }
        except requests.RequestException as e:
            return {"ok": False, "status": None, "error": str(e), "data": None}

    # ── TX Coordinator ────────────────────────────────────────────────────────

    def open_transaction(
        self,
        *,
        user_id: str,
        psa_terms: dict[str, Any],
        notes: str = "",
    ) -> dict[str, Any]:
        """
        Open a transaction file in deal-flow from a freshly-extracted PSA.

        `psa_terms` should match the shared.schemas.PSATerms shape in
        tranchi-deal-flow-agents:
            purchase_price, closing_date, buyer_name, seller_name,
            property_address (all required); plus optional earnest_money,
            inspection_period_days, financing_contingency_days,
            title_contingency_days, buyer/seller email+phone, agent names.

        Returns the standard {ok, data, status, error} dict. On success,
        `data` contains the deal-flow response (typically {tx_id, ...}).
        """
        payload = {
            "user_id": user_id,
            "psa_terms": psa_terms,
            "notes": notes,
        }
        return self._post("/api/tx/open", payload)

    def get_transaction(self, tx_id: str) -> dict[str, Any]:
        return self._get(f"/api/tx/{tx_id}")

    # ── Internals ─────────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = requests.post(
                f"{self.base_url}{path}",
                headers=self.headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {
                "ok": False,
                "status": None,
                "error": f"deal_flow transport error: {e}",
                "data": None,
            }
        ok = 200 <= resp.status_code < 300
        data = _safe_json(resp)
        return {
            "ok": ok,
            "status": resp.status_code,
            "data": data,
            "error": "" if ok else f"deal_flow {resp.status_code}",
        }

    def _get(self, path: str) -> dict[str, Any]:
        try:
            resp = requests.get(
                f"{self.base_url}{path}",
                headers=self.headers,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            return {
                "ok": False,
                "status": None,
                "error": f"deal_flow transport error: {e}",
                "data": None,
            }
        ok = 200 <= resp.status_code < 300
        data = _safe_json(resp)
        return {
            "ok": ok,
            "status": resp.status_code,
            "data": data,
            "error": "" if ok else f"deal_flow {resp.status_code}",
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text
