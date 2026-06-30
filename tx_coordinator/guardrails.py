"""
tx_coordinator/guardrails.py — safety rails for live-mode outbound.

Shadow mode logs what Sam *would* send and is inherently safe. Live mode
actually contacts counterparties, so every live dispatch must clear four gates
before it goes out. The sweeper calls `evaluate()` for each escalation it wants
to send live; a non-None return is the reason it was held back.

Gates (checked in this order — cheapest / most-decisive first):

  1. kill switch     — a DB flag (tx_settings) the operator can flip instantly,
                       no redeploy. When on, NOTHING live goes out.
  2. channel allowed — only the channels in TX_LIVE_CHANNELS may send. Voice is
                       deliberately excluded by default until text/email are
                       proven on real deals.
  3. quiet hours     — only send between TX_QUIET_START and TX_QUIET_END in
                       TX_QUIET_TZ. Outside the window the escalation is simply
                       skipped this tick and retried on the next in-window sweep
                       (no audit row written, so cooldown doesn't consume it).
  4. daily cap       — at most TX_DAILY_SEND_CAP successful live sends per rolling
                       24h, across all deals. Beyond that, hold until the window
                       clears.

Per-deal opt-in (global live is a master switch, each deal must also be flipped
live) lives in the sweeper, not here — see sweeper.run_sweep.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from shared.db import get_conn, fetchone

# ── Config (env-overridable; defaults match the agreed live-pilot policy) ──────

DAILY_SEND_CAP = int(os.environ.get("TX_DAILY_SEND_CAP", "3"))
# Channels permitted to actually send in live mode. "voice" is intentionally
# absent — autonomous AI calls are held back until text/email are trusted.
LIVE_CHANNELS = {
    c.strip()
    for c in os.environ.get("TX_LIVE_CHANNELS", "imessage,sms,email,arive").split(",")
    if c.strip()
}
QUIET_START_HOUR = int(os.environ.get("TX_QUIET_START", "8"))    # inclusive
QUIET_END_HOUR = int(os.environ.get("TX_QUIET_END", "20"))      # exclusive
QUIET_TZ = os.environ.get("TX_QUIET_TZ", "America/New_York")

KILL_SWITCH_KEY = "live_kill_switch"


# ── Kill switch (DB-backed, instant) ──────────────────────────────────────────


def kill_switch_active() -> bool:
    with get_conn() as conn:
        row = fetchone(conn, "SELECT value FROM tx_settings WHERE key = ?", (KILL_SWITCH_KEY,))
    return bool(row) and str(row.get("value")) == "1"


def set_kill_switch(on: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tx_settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (KILL_SWITCH_KEY, "1" if on else "0", now),
        )
        conn.commit()


# ── Individual gates ──────────────────────────────────────────────────────────


def channel_allowed(channel: str) -> bool:
    return channel in LIVE_CHANNELS


def within_quiet_hours(now: Optional[datetime] = None) -> bool:
    """True if RIGHT NOW is inside the allowed send window (i.e. OK to send)."""
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(QUIET_TZ))
    return QUIET_START_HOUR <= local.hour < QUIET_END_HOUR


def live_sends_last_24h(now: Optional[datetime] = None) -> int:
    """Count successful live sends (any deal) in the rolling 24h window."""
    ref = now or datetime.now(timezone.utc)
    cutoff = (ref - timedelta(hours=24)).isoformat()
    with get_conn() as conn:
        row = fetchone(
            conn,
            """SELECT COUNT(*) AS n FROM tx_outbound_messages
               WHERE mode = 'live'
                 AND (error IS NULL OR error = '')
                 AND sent_at > ?""",
            (cutoff,),
        )
    return int((row or {}).get("n", 0) or 0)


def cap_remaining(now: Optional[datetime] = None) -> int:
    return max(0, DAILY_SEND_CAP - live_sends_last_24h(now))


# ── Combined evaluation ───────────────────────────────────────────────────────


def evaluate(channel: str, now: Optional[datetime] = None) -> Optional[str]:
    """
    Return None if a live send on `channel` is allowed right now, otherwise a
    short skip-reason slug (used in the sweep summary). Order matters: the most
    decisive / global gates are checked first.
    """
    if kill_switch_active():
        return "kill_switch"
    if not channel_allowed(channel):
        return f"channel_disabled:{channel}"
    if not within_quiet_hours(now):
        return "quiet_hours"
    if live_sends_last_24h(now) >= DAILY_SEND_CAP:
        return "cap_reached"
    return None


def config_summary(now: Optional[datetime] = None) -> dict:
    """Human-readable snapshot for the /api/tx/guardrails endpoint."""
    used = live_sends_last_24h(now)
    return {
        "kill_switch_active": kill_switch_active(),
        "daily_send_cap": DAILY_SEND_CAP,
        "live_sends_last_24h": used,
        "cap_remaining": max(0, DAILY_SEND_CAP - used),
        "allowed_channels": sorted(LIVE_CHANNELS),
        "quiet_hours": {
            "start": QUIET_START_HOUR,
            "end": QUIET_END_HOUR,
            "tz": QUIET_TZ,
            "currently_in_window": within_quiet_hours(now),
        },
    }
