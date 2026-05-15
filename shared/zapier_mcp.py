"""
shared/zapier_mcp.py — Zapier MCP client seam (Tranchi-operator side).

This is the bridge between persona tool specs of the form
`zapier:<app>.<action>:<read|write>` and the actual Zapier MCP endpoint.

It speaks to a single Tranchi-account MCP server — i.e. it covers the
*operator* half of the hybrid action model (writes to Arive, company DocuSign
templates, the Tranchi inbox, internal Slack, QuickBooks, etc.). User-side
actions (the borrower's own Gmail/Calendar/DocuSign) go through per-user OAuth
in the production UI repo, NOT this client.

Configuration (all optional in dev — the client no-ops cleanly):
    ZAPIER_MCP_ENDPOINT   — full https://mcp.zapier.com/api/v1/mcp/... URL
    ZAPIER_MCP_API_KEY    — Zapier MCP API key for the Tranchi account

Usage:
    from shared.zapier_mcp import ZapierMCPClient, parse_handler

    app, action, mode = parse_handler("zapier:gmail.message:write")
    result = ZapierMCPClient().execute(app=app, action=action, mode=mode, params={...})
"""

from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any, Literal, Optional

ZAPIER_MCP_ENDPOINT = os.environ.get("ZAPIER_MCP_ENDPOINT", "").strip()
ZAPIER_MCP_API_KEY = os.environ.get("ZAPIER_MCP_API_KEY", "").strip()

Mode = Literal["read", "write"]


@dataclass(frozen=True)
class ParsedHandler:
    app: str       # e.g. "gmail"
    action: str    # e.g. "message"
    mode: Mode     # "read" or "write"


def parse_handler(handler: str) -> ParsedHandler:
    """
    Parse a tool handler string of the form `zapier:<app>.<action>:<read|write>`.

    Raises ValueError if the handler is not a Zapier handler or is malformed.
    """
    if not handler.startswith("zapier:"):
        raise ValueError(f"Not a Zapier handler: {handler!r}")
    _, body = handler.split(":", 1)
    try:
        app_action, mode = body.rsplit(":", 1)
        app, action = app_action.split(".", 1)
    except ValueError:
        raise ValueError(
            f"Malformed Zapier handler {handler!r} — expected "
            f"'zapier:<app>.<action>:<read|write>'"
        )
    if mode not in ("read", "write"):
        raise ValueError(f"Zapier handler mode must be 'read' or 'write', got {mode!r}")
    return ParsedHandler(app=app, action=action, mode=mode)  # type: ignore[arg-type]


class ZapierMCPClient:
    """
    Minimal client around the Tranchi-operator Zapier MCP endpoint.

    Wiring status: the transport (MCP over SSE/HTTP) is NOT yet implemented in
    this scaffolding. Calling `execute` raises NotImplementedError with the
    parsed action so the persona / orchestrator layer can be developed and
    tested against the persona's tool surface today. To complete the wiring:

      1. Install an MCP client (e.g. `mcp` Python package) or use the Zapier
         HTTP execution API for the chosen action.
      2. Implement `execute` to call the configured ZAPIER_MCP_ENDPOINT,
         translating (app, action, mode, params) into an MCP tool call.
      3. Return the structured result; errors should surface as exceptions so
         the chat runtime can decide whether to retry / fall back.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.endpoint = endpoint or ZAPIER_MCP_ENDPOINT
        self.api_key = api_key or ZAPIER_MCP_API_KEY

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.api_key)

    def execute(
        self,
        app: str,
        action: str,
        mode: Mode,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Execute a Zapier MCP action. NOT YET WIRED — see class docstring.
        """
        raise NotImplementedError(
            f"Zapier MCP execution not yet wired (app={app!r}, action={action!r}, "
            f"mode={mode!r}). See shared/zapier_mcp.py docstring for the contract."
        )
