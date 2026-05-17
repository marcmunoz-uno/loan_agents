"""
shared/browserbase.py — Browserbase cloud-browser sessions for co-pilot flows.

Used by the Tranchi - Working Capital persona to drive third-party rate-check
forms (SoFi, LightStream, Marcus, etc.) with the user watching via Browserbase's
live-view URL embedded in the Tranchi chat UI.

The agent fills non-sensitive fields (loan amount, name, address, income).
The user types SSN and clicks submit themselves in the live view — the SSN
never crosses Tranchi's servers.

Config:
    BROWSERBASE_API_KEY      — Browserbase API key
    BROWSERBASE_PROJECT_ID   — Browserbase project ID
    BROWSERBASE_TIMEOUT_S    — REST call timeout (default 30s)

Pattern (per rate-check session):
    1. open_session(geo_country="US") →
         {session_id, connect_url, live_view_url}
    2. Tranchi UI embeds `live_view_url` in an iframe — user watches the page.
    3. with playwright_page(connect_url) as page:
           page.goto(lender_url, ...)
           page.fill("input[name='loanAmount']", "50000")
           ... (all non-sensitive fields)
    4. User types SSN directly into the live view; clicks "Check My Rate".
    5. with playwright_page(connect_url) as page:
           page.wait_for_selector("[data-testid='rate-quote-amount']", ...)
           → capture the rate.
    6. stop_session(session_id).

Re-attaching a fresh Playwright per step is intentional — each call is a
short, stateless RPC into the live cloud browser. The browser itself stays
alive between calls for the user to keep watching.
"""

from __future__ import annotations
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import requests

BROWSERBASE_API_KEY = os.environ.get("BROWSERBASE_API_KEY", "").strip()
BROWSERBASE_PROJECT_ID = os.environ.get("BROWSERBASE_PROJECT_ID", "").strip()
BROWSERBASE_TIMEOUT_S = float(os.environ.get("BROWSERBASE_TIMEOUT_S", "30"))

_BROWSERBASE_BASE = "https://api.browserbase.com"


@dataclass
class Session:
    session_id: str
    connect_url: str       # CDP URL for Playwright.connect_over_cdp()
    live_view_url: str     # URL for the user-watchable / interactable iframe


def configured() -> bool:
    return bool(BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID)


def open_session(geo_country: str = "US", stealth: bool = True) -> Session:
    """
    Create a Browserbase cloud-browser session. Returns the CDP connect URL
    (for our Playwright calls) and the live-view URL (for the Tranchi UI to
    embed in an iframe so the user can watch + interact).
    """
    if not configured():
        raise RuntimeError(
            "Browserbase not configured — set BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID."
        )

    body = {
        "projectId": BROWSERBASE_PROJECT_ID,
        "browserSettings": {"stealth": stealth},
        "proxies": [{"type": "browserbase", "geolocation": {"country": geo_country}}],
    }
    resp = requests.post(
        f"{_BROWSERBASE_BASE}/v1/sessions",
        headers={"x-bb-api-key": BROWSERBASE_API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=BROWSERBASE_TIMEOUT_S,
    )
    if resp.status_code != 201:
        raise RuntimeError(
            f"Browserbase session create failed: {resp.status_code} {resp.text[:300]}"
        )
    data = resp.json()
    session_id = data["id"]
    return Session(
        session_id=session_id,
        connect_url=data["connectUrl"],
        live_view_url=_live_view_url(session_id) or "",
    )


def _live_view_url(session_id: str) -> Optional[str]:
    """
    Fetch the user-watchable live-view URL for an existing session.
    Browserbase exposes `debuggerFullscreenUrl` (interactive) and `debuggerUrl`
    (read-only) on the session debug endpoint.
    """
    try:
        resp = requests.get(
            f"{_BROWSERBASE_BASE}/v1/sessions/{session_id}/debug",
            headers={"x-bb-api-key": BROWSERBASE_API_KEY},
            timeout=BROWSERBASE_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("debuggerFullscreenUrl") or data.get("debuggerUrl")
    except Exception:
        return None


def stop_session(session_id: str) -> None:
    """Best-effort tear-down. Safe to call on an already-stopped session."""
    if not configured() or not session_id:
        return
    try:
        requests.post(
            f"{_BROWSERBASE_BASE}/v1/sessions/{session_id}",
            headers={"x-bb-api-key": BROWSERBASE_API_KEY, "Content-Type": "application/json"},
            json={"status": "REQUEST_RELEASE", "projectId": BROWSERBASE_PROJECT_ID},
            timeout=BROWSERBASE_TIMEOUT_S,
        )
    except Exception:
        pass


@contextmanager
def playwright_page(connect_url: str) -> Iterator:
    """
    Open a Playwright page attached to an existing Browserbase session.
    Yields the active tab in the cloud browser. We disconnect on exit but
    do NOT close the browser — the live view stays alive for the user.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(connect_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            yield page
        finally:
            try:
                browser.close()
            except Exception:
                pass
