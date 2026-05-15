"""
shared/zapier_mcp.py — Zapier MCP client (Tranchi-operator side).

Bridges persona tool specs of the form `zapier:<app>.<action>:<read|write>`
to the actual Zapier MCP server. The MCP server itself exposes meta-tools
(`execute_zapier_read_action`, `execute_zapier_write_action`) that take an
(app, action, params) triple plus natural-language `instructions` and
`output` hints — this client wraps that calling convention.

This client speaks to a single Tranchi-account MCP endpoint — the
*operator* half of the hybrid action model (writes to Arive, company
DocuSign templates, the Tranchi inbox, Tranchi calendar, internal Slack,
QuickBooks). User-side actions (the borrower's own Gmail/Calendar/etc.) go
through per-user OAuth in the production UI repo, NOT this client.

Configuration:
    ZAPIER_MCP_ENDPOINT  — full https URL of the Tranchi MCP endpoint, typically
                           ending in /sse (e.g. https://mcp.zapier.com/api/v1/mcp/<id>/sse).
                           Zapier embeds auth in the URL path for some plans;
                           ZAPIER_MCP_API_KEY is sent as Bearer for those that don't.
    ZAPIER_MCP_API_KEY   — optional bearer token sent in the Authorization header.

Transport: MCP over SSE via the official `mcp` Python SDK. The SDK is async,
so each call opens a session, runs the tool, and closes — fine for the
chat-turn-time call rate. If call volume grows, pool sessions.
"""

from __future__ import annotations
import asyncio
import os
from dataclasses import dataclass
from typing import Any, Literal, Optional

ZAPIER_MCP_ENDPOINT = os.environ.get("ZAPIER_MCP_ENDPOINT", "").strip()
ZAPIER_MCP_API_KEY = os.environ.get("ZAPIER_MCP_API_KEY", "").strip()
ZAPIER_MCP_TIMEOUT_S = float(os.environ.get("ZAPIER_MCP_TIMEOUT_S", "30"))

Mode = Literal["read", "write"]


@dataclass(frozen=True)
class ParsedHandler:
    app: str       # e.g. "gmail"
    action: str    # e.g. "message"
    mode: Mode     # "read" or "write"


def parse_handler(handler: str) -> ParsedHandler:
    """
    Parse a tool handler of the form `zapier:<app>.<action>:<read|write>`.
    Raises ValueError on malformed input.
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
            "'zapier:<app>.<action>:<read|write>'"
        )
    if mode not in ("read", "write"):
        raise ValueError(f"Zapier handler mode must be 'read' or 'write', got {mode!r}")
    return ParsedHandler(app=app, action=action, mode=mode)  # type: ignore[arg-type]


class ZapierMCPClient:
    """
    Async-backed client for the Tranchi-operator Zapier MCP endpoint.

    `execute()` is sync — it wraps the underlying async MCP call in
    `asyncio.run()` so Flask handlers (and the orchestrator dispatcher)
    can call it without going async themselves.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self.endpoint = endpoint or ZAPIER_MCP_ENDPOINT
        self.api_key = api_key or ZAPIER_MCP_API_KEY
        self.timeout_s = timeout_s or ZAPIER_MCP_TIMEOUT_S

    @property
    def configured(self) -> bool:
        # The endpoint URL is required. The API key may be embedded in the URL
        # path for some Zapier MCP plans, so it's not strictly required.
        return bool(self.endpoint)

    def execute(
        self,
        app: str,
        action: str,
        mode: Mode,
        params: dict[str, Any],
        instructions: Optional[str] = None,
        output: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Execute a Zapier MCP action. Returns the unwrapped tool result dict.

        Raises RuntimeError if not configured. Raises whatever the MCP SDK
        raises on transport / tool errors so the caller can decide policy.
        """
        if not self.configured:
            raise RuntimeError(
                "Zapier MCP not configured — set ZAPIER_MCP_ENDPOINT "
                "(and ZAPIER_MCP_API_KEY if your endpoint requires Bearer auth)."
            )
        meta_tool = "execute_zapier_write_action" if mode == "write" else "execute_zapier_read_action"
        return asyncio.run(self._call_async(
            meta_tool=meta_tool,
            args={
                "app": app,
                "action": action,
                "instructions": instructions or f"Run {app}.{action} for the Tranchi loan workflow.",
                "output": output or "Return the response in a structured form.",
                "params": params,
            },
        ))

    async def _call_async(self, meta_tool: str, args: dict[str, Any]) -> dict[str, Any]:
        # Import inside the function so the rest of the module loads even if
        # mcp isn't installed yet (e.g. dev environments without the dep).
        from mcp.client.sse import sse_client
        from mcp.client.session import ClientSession

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async def _run() -> dict[str, Any]:
            async with sse_client(url=self.endpoint, headers=headers) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    result = await session.call_tool(meta_tool, args)
                    return _unwrap(result)

        return await asyncio.wait_for(_run(), timeout=self.timeout_s)


def _unwrap(result: Any) -> dict[str, Any]:
    """
    Convert a mcp.CallToolResult into a plain dict the dispatcher can serialize.
    """
    # mcp.types.CallToolResult has .content (list of TextContent | ImageContent | ...) and .isError
    is_error = getattr(result, "isError", False)
    blocks: list[dict[str, Any]] = []
    raw_content = getattr(result, "content", None) or []
    for block in raw_content:
        block_dict: dict[str, Any]
        if hasattr(block, "model_dump"):
            block_dict = block.model_dump()
        elif hasattr(block, "__dict__"):
            block_dict = dict(block.__dict__)
        else:
            block_dict = {"type": "unknown", "repr": repr(block)}
        blocks.append(block_dict)
    return {"isError": bool(is_error), "content": blocks}
