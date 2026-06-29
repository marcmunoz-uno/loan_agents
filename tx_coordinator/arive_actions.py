"""
tx_coordinator/arive_actions.py — Outbound Arive actions via Zapier MCP.

There is no direct title-company integration in this stack. Title gets
ordered through Arive (the LO's loan-origination system); Arive forwards
the order to its configured title provider; status updates come back via
the existing `/api/loan/webhook/arive-update` endpoint in loan_agents (we
do NOT duplicate that handler here).

Pattern is copied from loan_agents/loan_officer/arive_create_loan.py:

    1. Build a params dict from the transaction's stored state
    2. Call ZapierMCPClient.execute(app="arive", action="order_title", ...)
    3. Audit the call into tx_outbound_messages so the sweeper / agent can
       see what was attempted and dedupe future tries

Action name `order_title` is a placeholder until the real Zapier action
name is wired — `_ARIVE_ACTION_ORDER_TITLE` is the single point to change.
The graceful-not-configured path returns `skipped:zapier_mcp_not_configured`
so dev works without a Zapier endpoint.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db import get_conn, fetchone, fetchall, insert
from shared.zapier_mcp import ZapierMCPClient

_ARIVE_APP = "arive"
_ARIVE_ACTION_ORDER_TITLE      = "order_title"
_ARIVE_ACTION_LIST_CONTACTS    = "list_loan_contacts"   # placeholder — confirm real Zapier action name
_ARIVE_ACTION_SEND_LOAN_EMAIL  = "send_loan_email"      # placeholder — confirm real Zapier action name

# Arive role-label → our tx_parties.party_type enum.
# Anything not in this map falls into party_type='other' on sync.
ARIVE_ROLE_MAP: dict[str, str] = {
    # Buyer side
    "Borrower":                 "buyer",
    "Co-Borrower":              "buyer",
    "Buyer":                    "buyer",
    "Buyer's Agent":            "buyer_agent",
    "Buyer Agent":              "buyer_agent",
    "Selling Agent":            "buyer_agent",
    # Seller side
    "Seller":                   "seller",
    "Listing Agent":            "listing_agent",
    "Seller's Agent":           "listing_agent",
    # Service providers
    "Title Company":            "title",
    "Title":                    "title",
    "Title Officer":            "title",
    "Escrow":                   "escrow",
    "Escrow Officer":           "escrow",
    "Closer":                   "escrow",
    "Settlement Agent":         "escrow",
    "Home Inspector":           "inspector",
    "Inspector":                "inspector",
    "Lender":                   "lender",
    "Loan Officer":             "lender",
    "Mortgage Broker":          "lender",
    "Insurance Agent":          "insurance",
    "Insurance":                "insurance",
}


# ── Public entry point ────────────────────────────────────────────────────────


def order_title_through_arive(
    tx_id: str,
    *,
    force: bool = False,
    client: Optional[ZapierMCPClient] = None,
) -> dict[str, Any]:
    """
    Fire arive.order_title for `tx_id`. Idempotent against
    `tx_outbound_messages.reason == "title_ordered_via_arive"`: subsequent
    calls return `skipped:already_ordered` unless `force=True`.

    Returns:
        {
          "ok":            bool,
          "status":        "sent" | "skipped:..." | "failed:...",
          "tx_id":         str,
          "params_sent":   dict,
          "response":      dict | None,
          "audit_row_id":  int | None,
        }
    """
    with get_conn() as conn:
        tx = fetchone(conn, "SELECT * FROM transactions WHERE id = ?", (tx_id,))
    if not tx:
        return _result(False, "failed:transaction_not_found", tx_id, {}, None, None)
    if not force and _already_ordered(tx_id):
        return _result(True, "skipped:already_ordered", tx_id, {}, None, None)

    try:
        psa = json.loads(tx.get("psa_terms") or "{}")
    except json.JSONDecodeError:
        psa = {}
    parties = _party_map(tx_id)
    params = build_title_order_params(tx=tx, psa=psa, parties=parties)

    cli = client or ZapierMCPClient()
    if not cli.configured:
        audit_id = _audit(tx_id, params=params, mode="shadow",
                          outbound_ref="", error="zapier_mcp_not_configured")
        return _result(False, "skipped:zapier_mcp_not_configured", tx_id, params, None, audit_id)

    try:
        response = cli.execute(
            app=_ARIVE_APP,
            action=_ARIVE_ACTION_ORDER_TITLE,
            mode="write",
            params=params,
            instructions=(
                "Place a title order in Arive for the executed PSA. Arive will "
                "forward the order to its configured title provider. Use the "
                "purchase price, closing date, and property address from the "
                "transaction record."
            ),
            output="Return any Arive title-order ID and confirmation status.",
        )
    except Exception as e:  # noqa: BLE001 — surface as a structured failure
        audit_id = _audit(tx_id, params=params, mode="live",
                          outbound_ref="", error=f"{type(e).__name__}: {e}")
        return _result(False, f"failed:{type(e).__name__}", tx_id, params, None, audit_id)

    ok = not (isinstance(response, dict) and response.get("isError"))
    arive_ref = _extract_ref(response) if isinstance(response, dict) else ""
    audit_id = _audit(tx_id, params=params, mode="live",
                      outbound_ref=arive_ref, error="" if ok else "arive_returned_error")
    return _result(ok, "sent" if ok else "failed:arive_error", tx_id, params, response, audit_id)


# ── Param builder (Arive payload) ─────────────────────────────────────────────


def build_title_order_params(
    *,
    tx: dict,
    psa: dict,
    parties: dict,
) -> dict[str, Any]:
    """
    Convert our stored tx + PSA + parties into the params Arive's order_title
    action expects. Field names mirror `arive_create_loan.map_to_arive_params`
    so anyone reading both modules sees the same vocabulary.

    Only includes fields with non-empty values — Arive applies its own
    defaults where we omit data.
    """
    buyer = parties.get("buyer", {})
    seller = parties.get("seller", {})
    listing_agent = parties.get("listing_agent", {})

    first, last = _split_name(buyer.get("name") or tx.get("buyer_name", ""))
    addr = _parse_address(tx.get("property_address", "") or psa.get("property_address", ""))

    purchase_price = _coerce_money(tx.get("purchase_price"))
    earnest_money = _coerce_money(psa.get("earnest_money"))

    params: dict[str, Any] = {
        # Provenance — lets Arive / Zapier dedupe retries.
        "crmReferenceId":     tx["id"],
        "externalCreateDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "loanPurpose":        "Purchase",
        # Borrower / property summary so Arive can match an existing loan.
        "borrower1_firstName": first or "Borrower",
        "borrower1_lastName":  last or "Unknown",
    }
    if buyer.get("email"):
        params["borrower1_emailAddressText"] = buyer["email"]
    if buyer.get("phone"):
        params["borrower1_mobilePhone10digit"] = _coerce_phone10(buyer["phone"])

    if addr["addressLineText"]:
        params["subjectProperty_addressLineText"] = addr["addressLineText"]
    if addr["city"]:
        params["subjectProperty_city"] = addr["city"]
    if addr["state"]:
        params["subjectProperty_state"] = addr["state"]
    if addr["postalCode"]:
        params["subjectProperty_postalCode"] = addr["postalCode"]

    if purchase_price > 0:
        params["purchasePriceOrEstimatedValue"] = round(purchase_price, 2)
    if earnest_money > 0:
        params["earnestMoneyDepositAmount"] = round(earnest_money, 2)

    if tx.get("closing_date"):
        params["estimatedClosingDate"] = tx["closing_date"]
    if tx.get("psa_execution_date"):
        params["contractExecutionDate"] = tx["psa_execution_date"]

    # Helpful counterparty context Arive may forward to the title shop.
    if seller.get("name"):
        params["seller1_fullName"] = seller["name"]
    if seller.get("email"):
        params["seller1_emailAddressText"] = seller["email"]
    if listing_agent.get("name"):
        params["listingAgentName"] = listing_agent["name"]

    return params


# ── Internals ─────────────────────────────────────────────────────────────────


def _party_map(tx_id: str) -> dict[str, dict]:
    with get_conn() as conn:
        rows = fetchall(conn, "SELECT * FROM tx_parties WHERE transaction_id = ?", (tx_id,))
    return {r["party_type"]: r for r in rows}


def _already_ordered(tx_id: str) -> bool:
    with get_conn() as conn:
        row = fetchone(
            conn,
            """SELECT 1 FROM tx_outbound_messages
               WHERE transaction_id = ? AND reason = 'title_ordered_via_arive'
                 AND error = '' LIMIT 1""",
            (tx_id,),
        )
    return row is not None


def _audit(
    tx_id: str,
    *,
    params: dict,
    mode: str,
    outbound_ref: str,
    error: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        return insert(conn, "tx_outbound_messages", {
            "transaction_id": tx_id,
            "party_id":       None,
            "target_role":    "arive",
            "channel":        "arive",
            "reason":         "title_ordered_via_arive",
            "body":           json.dumps(params)[:4000],
            "mode":           mode,
            "outbound_ref":   outbound_ref,
            "sent_at":        now,
            "error":          error,
        })


def _result(
    ok: bool,
    status: str,
    tx_id: str,
    params: dict,
    response: Optional[dict],
    audit_row_id: Optional[int],
) -> dict[str, Any]:
    return {
        "ok":           ok,
        "status":       status,
        "tx_id":        tx_id,
        "params_sent":  params,
        "response":     response,
        "audit_row_id": audit_row_id,
    }


_REF_PATTERNS = [
    re.compile(r'"title_?[Oo]rder_?[Ii]d"\s*:\s*"?([A-Za-z0-9_-]{4,})"?'),
    re.compile(r'"order_?[Ii]d"\s*:\s*"?([A-Za-z0-9_-]{4,})"?'),
    re.compile(r'"id"\s*:\s*"?([A-Za-z0-9_-]{4,})"?'),
]


def _extract_ref(response: dict) -> str:
    for block in response.get("content") or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or ""
        for pat in _REF_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(1)
    return ""


def _split_name(full: str) -> tuple[str, str]:
    if not full:
        return ("", "")
    head = full.split(",", 1)[0].strip()
    parts = head.split()
    if not parts:
        return ("", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (" ".join(parts[:-1]), parts[-1])


_STATE_ZIP_TAIL_RE = re.compile(r"^(.+?)\s+([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")
_STATE_ZIP_RE      = re.compile(r"^([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")


def _parse_address(address: str) -> dict[str, str]:
    """
    Best-effort US-address parse. Accepts either:
        "<street>, <city>, <STATE> <ZIP>"        — Arive create_loan PSA format
        "<street>, <city> <STATE> <ZIP>"         — Tranchi seed/short format
    """
    out = {"addressLineText": "", "city": "", "state": "", "postalCode": ""}
    if not address:
        return out
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return out
    out["addressLineText"] = parts[0]

    tail = parts[-1] if len(parts) >= 2 else ""
    # Try "<STATE> <ZIP>" alone (3+ comma-separated form).
    m = _STATE_ZIP_RE.match(tail)
    if m:
        out["state"] = m.group(1)
        out["postalCode"] = m.group(2)
        if len(parts) >= 3:
            out["city"] = parts[-2]
        return out
    # Try "<city> <STATE> <ZIP>" (2-segment form).
    m = _STATE_ZIP_TAIL_RE.match(tail)
    if m:
        out["city"] = m.group(1)
        out["state"] = m.group(2)
        out["postalCode"] = m.group(3)
        return out
    if len(parts) >= 2:
        out["city"] = parts[1]
    return out


def _coerce_money(value: Any) -> float:
    if value in (None, "", 0):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        cleaned = re.sub(r"[^\d.]", "", str(value))
        return float(cleaned) if cleaned else 0.0


def _coerce_phone10(value: Any) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    return digits[-10:] if len(digits) >= 10 else ""


# ── Contact sync — pulls the party roster from Arive ──────────────────────────


def sync_parties_from_arive(
    tx_id: str,
    *,
    client: Optional[ZapierMCPClient] = None,
) -> dict[str, Any]:
    """
    Refresh `tx_parties` from Arive's `list_loan_contacts` action.

    Arive is the source of truth for who's tagged on the deal — the LO enters
    parties there when creating the loan, and any reshuffle (new title shop,
    swapped buyer's agent) happens in the LOS. We mirror into tx_parties
    rather than read-through so the sweeper's joined queries (silent-party
    detection, escalation routing) stay fast.

    Upsert semantics:
      - Rows with source='agent' are NEVER deleted by sync. Sam-added parties
        survive every refresh.
      - Rows with source='arive' are upserted keyed by (transaction_id,
        arive_contact_id). If a contact disappears from Arive, we mark
        synced_at to "now" but leave the row — caller can prune separately
        if needed.

    Returns: {ok, status, synced_count, skipped_count, error}
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        tx = fetchone(conn, "SELECT arive_loan_id FROM transactions WHERE id = ?", (tx_id,))
    if not tx:
        return {"ok": False, "status": "failed:transaction_not_found", "synced_count": 0,
                "skipped_count": 0, "error": ""}
    loan_id = (tx or {}).get("arive_loan_id") or ""
    if not loan_id:
        return {"ok": False, "status": "skipped:no_arive_loan_id", "synced_count": 0,
                "skipped_count": 0, "error": ""}

    cli = client or ZapierMCPClient()
    if not cli.configured:
        return {"ok": False, "status": "skipped:zapier_mcp_not_configured",
                "synced_count": 0, "skipped_count": 0, "error": ""}

    try:
        response = cli.execute(
            app=_ARIVE_APP,
            action=_ARIVE_ACTION_LIST_CONTACTS,
            mode="read",
            params={"loanId": loan_id},
            instructions="List every contact tagged on this Arive loan with their role, name, email, and phone.",
            output="Return a JSON array of {role, name, email, phone, arive_contact_id, company}.",
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": f"failed:{type(e).__name__}",
                "synced_count": 0, "skipped_count": 0, "error": str(e)[:300]}

    contacts = _extract_contacts(response)
    synced = 0
    skipped = 0
    with get_conn() as conn:
        for c in contacts:
            arive_id = (c.get("arive_contact_id") or c.get("id") or "").strip()
            role_label = (c.get("role") or "").strip()
            party_type = ARIVE_ROLE_MAP.get(role_label, "other")
            name = (c.get("name") or "").strip()
            if not name or not arive_id:
                skipped += 1
                continue

            existing = fetchone(
                conn,
                "SELECT id FROM tx_parties WHERE transaction_id = ? AND arive_contact_id = ?",
                (tx_id, arive_id),
            )
            if existing:
                conn.execute(
                    """UPDATE tx_parties
                       SET party_type = ?, name = ?, email = ?, phone = ?, company = ?,
                           source = 'arive', synced_at = ?
                       WHERE id = ?""",
                    (party_type, name, c.get("email", ""), c.get("phone", ""),
                     c.get("company", ""), now, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO tx_parties
                       (transaction_id, party_type, name, email, phone, company,
                        source, arive_contact_id, added_at, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'arive', ?, ?, ?)""",
                    (tx_id, party_type, name, c.get("email", ""), c.get("phone", ""),
                     c.get("company", ""), arive_id, now, now),
                )
            synced += 1
        conn.commit()

    return {"ok": True, "status": "synced", "synced_count": synced,
            "skipped_count": skipped, "error": ""}


# ── Loan email — posts an email inside the Arive loan file ────────────────────


def post_loan_update(
    tx_id: str,
    *,
    subject: str,
    body: str,
    client: Optional[ZapierMCPClient] = None,
) -> dict[str, Any]:
    """
    Fire arive.send_loan_email so every contact tagged on the loan file
    receives the email AND it's archived inside Arive for audit.

    Does NOT dedupe — callers (notifications.notify_deal, the agent tool,
    the milestone-completion allowlist) are responsible for deciding when
    to send. Every call writes a fresh audit row.

    Returns: {ok, status, tx_id, audit_row_id, response}
    """
    with get_conn() as conn:
        tx = fetchone(conn, "SELECT * FROM transactions WHERE id = ?", (tx_id,))
    if not tx:
        return {"ok": False, "status": "failed:transaction_not_found",
                "tx_id": tx_id, "audit_row_id": None, "response": None}
    loan_id = tx.get("arive_loan_id") or ""

    cli = client or ZapierMCPClient()
    params = {
        "loanId":  loan_id,
        "subject": subject,
        "body":    body,
        "crmReferenceId": tx_id,
    }

    if not cli.configured:
        audit_id = _audit_loan_email(tx_id, subject=subject, body=body, mode="shadow",
                                     outbound_ref="", error="zapier_mcp_not_configured")
        return {"ok": False, "status": "skipped:zapier_mcp_not_configured",
                "tx_id": tx_id, "audit_row_id": audit_id, "response": None}

    if not loan_id:
        audit_id = _audit_loan_email(tx_id, subject=subject, body=body, mode="shadow",
                                     outbound_ref="", error="no_arive_loan_id")
        return {"ok": False, "status": "skipped:no_arive_loan_id",
                "tx_id": tx_id, "audit_row_id": audit_id, "response": None}

    try:
        response = cli.execute(
            app=_ARIVE_APP,
            action=_ARIVE_ACTION_SEND_LOAN_EMAIL,
            mode="write",
            params=params,
            instructions=(
                "Send this email through Arive so every contact tagged on the "
                "loan receives it and the message is archived inside the loan file."
            ),
            output="Return any email message id and confirmation status.",
        )
    except Exception as e:  # noqa: BLE001
        audit_id = _audit_loan_email(tx_id, subject=subject, body=body, mode="live",
                                     outbound_ref="", error=f"{type(e).__name__}: {e}")
        return {"ok": False, "status": f"failed:{type(e).__name__}",
                "tx_id": tx_id, "audit_row_id": audit_id, "response": None}

    ok = not (isinstance(response, dict) and response.get("isError"))
    ref = _extract_ref(response) if isinstance(response, dict) else ""
    audit_id = _audit_loan_email(tx_id, subject=subject, body=body, mode="live",
                                 outbound_ref=ref, error="" if ok else "arive_returned_error")
    return {"ok": ok, "status": "sent" if ok else "failed:arive_error",
            "tx_id": tx_id, "audit_row_id": audit_id, "response": response}


def _audit_loan_email(
    tx_id: str,
    *,
    subject: str,
    body: str,
    mode: str,
    outbound_ref: str,
    error: str,
) -> int:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps({"subject": subject, "body": body})
    with get_conn() as conn:
        return insert(conn, "tx_outbound_messages", {
            "transaction_id": tx_id,
            "party_id":       None,
            "target_role":    "arive",
            "channel":        "arive",
            "reason":         "loan_file_email",
            "body":           payload[:4000],
            "mode":           mode,
            "outbound_ref":   outbound_ref,
            "sent_at":        now,
            "error":          error,
        })


def _extract_contacts(response: Any) -> list[dict[str, Any]]:
    """
    Pull a flat list of contact dicts from whatever shape Arive's MCP returns.
    Tries: top-level list, response['content'][i]['text'] containing JSON.
    """
    if isinstance(response, list):
        return [c for c in response if isinstance(c, dict)]
    if not isinstance(response, dict):
        return []
    # Direct field
    for key in ("contacts", "loan_contacts", "team", "parties"):
        v = response.get(key)
        if isinstance(v, list):
            return [c for c in v if isinstance(c, dict)]
    # MCP-style content blocks
    for block in response.get("content") or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or ""
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            return [c for c in parsed if isinstance(c, dict)]
        if isinstance(parsed, dict):
            for key in ("contacts", "loan_contacts", "team", "parties"):
                v = parsed.get(key)
                if isinstance(v, list):
                    return [c for c in v if isinstance(c, dict)]
    return []
