"""
loan_officer/intake/routes.py — Flask blueprint: /api/intake/*

The intake doc pipeline's HTTP surface. All endpoints require
`Authorization: Bearer <TRANCHI_API_SECRET>` except `/inbound-email-attachment`
which authenticates via a separate `ZAPIER_INBOUND_SECRET`.

Endpoints
---------
POST   /api/intake/upload/presign         — reserve a doc row, return presigned PUT URL
POST   /api/intake/upload/confirm         — borrower's PUT finished; verify in S3
POST   /api/intake/upload/<doc_id>/classify — synchronously OCR a doc (Claude vision)
GET    /api/intake/upload/<doc_id>        — current upload + classification status
GET    /api/intake/deals/<deal_id>/docs   — all intake docs for a borrower deal
GET    /api/intake/applications/<app_id>/docs — all intake docs attached to an app
GET    /api/intake/applications/<app_id>/completeness?product=dscr — doc-checklist gap report
POST   /api/intake/upload/<doc_id>/attach — wire an intake doc to a loan_application
POST   /api/intake/inbound-email-attachment — Zapier-fired webhook: borrower replied to
                                              the prequal letter with the executed PSA
                                              (or any other doc) attached. Matches by
                                              sender email, classifies, and — if it's a
                                              purchase_contract — auto-opens the deal-flow
                                              transaction.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import requests
from flask import Blueprint, request, jsonify

from shared.db import get_conn, fetchone, fetchall, insert, update

from shared.auth import require_tranchi_auth
from shared.s3_client import S3NotConfigured, get_default_client

from loan_officer.intake.upload import (
    UploadError,
    presign_upload,
    confirm_upload,
    get_upload_status,
    list_docs_for_deal,
    list_docs_for_application,
    attach_to_application,
)
from loan_officer.intake.ocr_classifier import (
    ClassifierError,
    DocumentClassifier,
)
from loan_officer.intake.completeness import check_completeness, gap_message


intake_bp = Blueprint("intake", __name__, url_prefix="/api/intake")


# ── /upload/presign ───────────────────────────────────────────────────────────

@intake_bp.route("/upload/presign", methods=["POST"])
@require_tranchi_auth
def presign():
    body = request.get_json(silent=True) or {}
    try:
        result = presign_upload(
            deal_id=str(body.get("deal_id", "")),
            filename=str(body.get("filename", "")),
            content_type=str(body.get("content_type", "")),
            user_id=str(body.get("user_id", "")),
            declared_doc_type=str(body.get("declared_doc_type", "")),
            application_id=str(body.get("application_id", "")),
        )
    except UploadError as e:
        return jsonify({"error": str(e)}), 400
    except S3NotConfigured as e:
        return jsonify({"error": str(e)}), 503
    return jsonify(result)


# ── /upload/confirm ───────────────────────────────────────────────────────────

@intake_bp.route("/upload/confirm", methods=["POST"])
@require_tranchi_auth
def confirm():
    body = request.get_json(silent=True) or {}
    doc_id = str(body.get("doc_id", ""))
    if not doc_id:
        return jsonify({"error": "doc_id is required"}), 400
    try:
        row = confirm_upload(
            doc_id=doc_id,
            size_bytes=int(body.get("size_bytes", 0) or 0),
            verify_in_s3=bool(body.get("verify_in_s3", True)),
        )
    except UploadError as e:
        return jsonify({"error": str(e)}), 404 if "unknown doc_id" in str(e) else 400
    return jsonify(row)


# ── /upload/<doc_id>/classify ─────────────────────────────────────────────────

@intake_bp.route("/upload/<doc_id>/classify", methods=["POST"])
@require_tranchi_auth
def classify(doc_id: str):
    """
    Run the Claude vision classifier on a single uploaded doc, synchronously.
    Returns the ClassificationResult dict (and the row is persisted with status='classified').

    Side effect: if this classification flips completeness to is_complete=True
    for the doc's application and no letter has been sent in the last 24h,
    auto-generates + sends the pre-qualification letter. The response includes
    an `autofired_letter` block when that happens.
    """
    try:
        result = DocumentClassifier().classify(doc_id)
    except ClassifierError as e:
        msg = str(e).lower()
        code = 404 if "unknown doc_id" in msg else 409 if "not ready" in msg else 400
        return jsonify({"error": str(e)}), code
    except S3NotConfigured as e:
        return jsonify({"error": str(e)}), 503
    except RuntimeError as e:
        # Bubble up vision / Anthropic errors as 502 — upstream dependency failed.
        return jsonify({"error": str(e)}), 502

    response: dict[str, Any] = result.to_dict()
    autofired = _maybe_autofire_prequal_letter(doc_id)
    if autofired:
        response["autofired_letter"] = autofired

    # PSA-specific side effects: open the TX in deal-flow AND create the
    # loan record in Arive (which sends the borrower the 1003 invitation).
    # Both are independent — either failing doesn't block the other.
    if result.doc_type == "purchase_contract":
        tx_result = _maybe_open_transaction(doc_id, result.extracted_fields)
        if tx_result:
            response["autofired_transaction"] = tx_result

        invite_result = _maybe_send_loan_app_invitation(doc_id, result.extracted_fields)
        if invite_result:
            response["autofired_loan_app_invitation"] = invite_result

    return jsonify(response)


# ── /upload/<doc_id> (status) ─────────────────────────────────────────────────

@intake_bp.route("/upload/<doc_id>", methods=["GET"])
@require_tranchi_auth
def status(doc_id: str):
    row = get_upload_status(doc_id)
    if row is None:
        return jsonify({"error": "doc not found"}), 404
    return jsonify(row)


# ── /upload/<doc_id>/attach ───────────────────────────────────────────────────

@intake_bp.route("/upload/<doc_id>/attach", methods=["POST"])
@require_tranchi_auth
def attach(doc_id: str):
    body = request.get_json(silent=True) or {}
    application_id = str(body.get("application_id", ""))
    if not application_id:
        return jsonify({"error": "application_id is required"}), 400
    row = attach_to_application(doc_id, application_id)
    if row is None:
        return jsonify({"error": "doc not found"}), 404
    return jsonify(row)


# ── /deals/<deal_id>/docs ─────────────────────────────────────────────────────

@intake_bp.route("/deals/<deal_id>/docs", methods=["GET"])
@require_tranchi_auth
def docs_by_deal(deal_id: str):
    return jsonify({"deal_id": deal_id, "docs": list_docs_for_deal(deal_id)})


# ── /applications/<app_id>/docs ──────────────────────────────────────────────

@intake_bp.route("/applications/<app_id>/docs", methods=["GET"])
@require_tranchi_auth
def docs_by_application(app_id: str):
    return jsonify({"application_id": app_id, "docs": list_docs_for_application(app_id)})


# ── /applications/<app_id>/completeness ──────────────────────────────────────

# ── Auto-trigger helpers ──────────────────────────────────────────────────────

_LETTER_DEDUP_SECONDS = 24 * 60 * 60  # 24h

# A required doc only counts toward completeness if Claude vision actually
# classified it as that type with reasonable confidence. We never satisfy a
# required slot from the borrower's self-declared label — that would let three
# mislabeled junk uploads fire an approval letter.
_MIN_CLASSIFY_CONFIDENCE = 0.70

# Minimum liquidity before the autonomous path will mail an approval letter.
_MIN_LIQUID_FOR_LETTER = 5_000.0


def _claim_letter_slot(application_id: str, window_seconds: int = _LETTER_DEDUP_SECONDS) -> bool:
    """
    Atomically claim the right to send a letter for this application within the
    dedup window. Returns True if the caller won the claim, False if another
    request already holds it.

    Uses BEGIN IMMEDIATE so concurrent classify→letter calls across gunicorn
    workers serialize on the write lock — closes the TOCTOU race that could
    otherwise mail a borrower two letters.
    """
    if not application_id:
        return False
    now = time.time()
    conn = get_conn()
    try:
        conn.isolation_level = None  # manual transaction control
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT claimed_at FROM letter_claims WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        if row:
            try:
                last = float(row["claimed_at"])
            except (TypeError, ValueError):
                last = 0.0
            if now - last < window_seconds:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "UPDATE letter_claims SET claimed_at = ? WHERE application_id = ?",
                (str(now), application_id),
            )
        else:
            conn.execute(
                "INSERT INTO letter_claims (application_id, claimed_at) VALUES (?, ?)",
                (application_id, str(now)),
            )
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def _release_letter_slot(application_id: str) -> None:
    """Release a claim after a failed send so a transient error doesn't block
    retries for the full dedup window."""
    if not application_id:
        return
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM letter_claims WHERE application_id = ?", (application_id,))
            conn.commit()
    except Exception:
        pass


def _maybe_autofire_prequal_letter(doc_id: str) -> Optional[dict[str, Any]]:
    """
    After a successful classify(), check whether the doc's application is now
    complete and no letter has been sent in the last 24h. If both: generate +
    auto-send and return a summary block. Otherwise None.

    Returns:
        {"letter_id", "max_pp_low", "max_pp_high", "mcp_send_status",
         "zap_fired", "pdf_url"}  on fire
        {"skipped": "<reason>"}                                          on skip
        None                                                              if nothing applicable
    """
    # 1. Find the application + prequal this doc belongs to
    doc = get_upload_status(doc_id)
    if not doc or not doc.get("application_id"):
        return None
    app_id = doc["application_id"]

    with get_conn() as conn:
        app_row = fetchone(
            conn,
            "SELECT id, prequal_id FROM loan_applications WHERE id = ?",
            (app_id,),
        )
    if not app_row or not app_row.get("prequal_id"):
        return None
    prequal_id = app_row["prequal_id"]

    # 2. Resolve the product the borrower is pursuing
    with get_conn() as conn:
        pq_row = fetchone(
            conn,
            "SELECT suggested_product FROM loan_prequals WHERE id = ?",
            (prequal_id,),
        )
    product = (pq_row.get("suggested_product") if pq_row else "dscr") or "dscr"

    # 3. Run completeness against current intake state. A required doc only
    #    counts if it was actually CLASSIFIED as that type with sufficient
    #    confidence — never from the borrower's self-declared label, and never
    #    from a low-confidence guess. This stops mislabeled / misread uploads
    #    from firing an approval letter.
    docs = list_docs_for_application(app_id)
    received = []
    for d in docs:
        if d.get("status") != "classified":
            continue
        try:
            conf = float(d.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < _MIN_CLASSIFY_CONFIDENCE:
            continue
        dt = (d.get("classified_doc_type") or "").strip()
        if dt:
            received.append(dt)
    report = check_completeness(product, received)
    if not report.is_complete:
        return None

    from loan_officer.prequal_letter import generate_and_send

    # 4. Atomically claim the letter slot — dedups across concurrent classify
    #    calls AND across the 24h window in one race-safe step.
    if not _claim_letter_slot(app_id):
        return {"skipped": "letter_already_sent_or_in_flight"}

    # 5. Fire. On failure, release the claim so a transient error doesn't block
    #    retries for 24h.
    try:
        letter = generate_and_send(prequal_id, min_liquid=_MIN_LIQUID_FOR_LETTER)
    except Exception as e:
        _release_letter_slot(app_id)
        return {"skipped": f"generate_failed:{e}"}

    return {
        "letter_id":        letter.letter_id,
        "max_pp_low":       letter.max_pp_low,
        "max_pp_high":      letter.max_pp_high,
        "mcp_send_status":  letter.mcp_send_status,
        "zap_fired":        letter.zap_fired,
        "pdf_url":          letter.pdf_url,
    }


# ── PSA → TX Coordinator handoff ──────────────────────────────────────────────

def _maybe_open_transaction(doc_id: str, extracted_fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    When a doc classifies as purchase_contract, build PSATerms from the extracted
    fields and POST to deal-flow's /api/tx/open. Returns a summary dict or None
    if the doc isn't tied to a borrower we can resolve.

    Idempotency: if intake_documents already has a tx_id on this doc, skip.
    """
    doc = get_upload_status(doc_id)
    if not doc:
        return None

    # Already opened a transaction for this doc?
    extra_blob = doc.get("extracted_fields") or {}
    if isinstance(extra_blob, dict) and extra_blob.get("_tx_id"):
        return {"skipped": f"tx_already_opened:{extra_blob['_tx_id']}"}

    # Find the borrower's user_id — prefer the doc's stamped user_id, fall back
    # to the linked application's prequal.
    user_id = doc.get("user_id") or ""
    if not user_id and doc.get("application_id"):
        with get_conn() as conn:
            app_row = fetchone(
                conn,
                "SELECT lp.user_id FROM loan_applications la "
                "LEFT JOIN loan_prequals lp ON lp.id = la.prequal_id "
                "WHERE la.id = ?",
                (doc["application_id"],),
            )
        if app_row:
            user_id = app_row.get("user_id") or ""

    if not user_id:
        return {"skipped": "no_user_id_for_doc"}

    # Build PSATerms — required fields from PSATerms in deal-flow's shared.schemas
    required_keys = ("purchase_price", "closing_date",
                     "buyer_name", "seller_name", "property_address")
    psa = {}
    for k in required_keys:
        v = extracted_fields.get(k)
        if v is None or v == "":
            return {"skipped": f"missing_required_field:{k}", "extracted": extracted_fields}
        psa[k] = v
    psa["purchase_price"] = _coerce_money(psa["purchase_price"])
    psa["closing_date"]   = _normalize_date(psa["closing_date"])
    if psa["purchase_price"] is None or psa["closing_date"] is None:
        return {"skipped": "could_not_coerce_purchase_price_or_closing_date",
                "extracted": extracted_fields}

    # Optional fields the classifier sometimes extracts:
    for opt in ("earnest_money", "inspection_period_days",
                "financing_contingency_days", "title_contingency_days",
                "seller_concessions", "buyer_email", "buyer_phone",
                "seller_email", "seller_phone",
                "buyer_agent_name", "listing_agent_name"):
        if opt in extracted_fields and extracted_fields[opt] not in (None, ""):
            psa[opt] = extracted_fields[opt]

    psa["notes"] = f"Auto-opened from intake doc {doc_id}"

    # Call deal-flow
    from shared.deal_flow_client import DealFlowClient
    client = DealFlowClient()
    result = client.open_transaction(
        user_id=user_id,
        psa_terms=psa,
        notes=psa["notes"],
    )
    if not result.get("ok"):
        return {
            "skipped": f"deal_flow_call_failed:{result.get('status') or result.get('error')}",
            "deal_flow_response": result.get("data"),
        }

    tx_id = ""
    data = result.get("data") or {}
    if isinstance(data, dict):
        tx_id = data.get("tx_id") or data.get("transaction_id") or ""

    # Persist tx_id back on the intake doc (in extracted_fields blob)
    if tx_id:
        merged = dict(extra_blob) if isinstance(extra_blob, dict) else {}
        merged["_tx_id"] = tx_id
        with get_conn() as conn:
            update(conn, "intake_documents", {
                "extracted_fields": json.dumps(merged),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, "doc_id = ?", (doc_id,))

    return {"tx_id": tx_id, "deal_flow_status": result.get("status"), "psa_terms": psa}


def _maybe_send_loan_app_invitation(doc_id: str, extracted_fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    Email the borrower their 1003 registration link after a PSA arrives.

    The Munoz firm uses an external POS at https://2589631.my1003app.com/0/register
    where borrowers self-register + fill out the URLA Form 1003. Once they
    submit, Arive picks up the application via its standard POS workflow.

    This auto-fires from the PSA classification path so the LO doesn't have
    to manually send the link. Idempotent — stamps `_loan_app_invitation_sent`
    on the intake doc so re-classifies don't double-send.
    """
    doc = get_upload_status(doc_id)
    if not doc:
        return None

    extra_blob = doc.get("extracted_fields") or {}
    if isinstance(extra_blob, dict) and extra_blob.get("_loan_app_invitation_sent"):
        return {"skipped": f"invitation_already_sent:{extra_blob['_loan_app_invitation_sent']}"}

    # Resolve the borrower's prequal
    user_id = doc.get("user_id") or ""
    app_id = doc.get("application_id") or ""
    prequal_id = ""
    if app_id:
        with get_conn() as conn:
            row = fetchone(conn, "SELECT prequal_id FROM loan_applications WHERE id = ?", (app_id,))
        if row:
            prequal_id = row.get("prequal_id") or ""
    if not prequal_id:
        return {"skipped": "no_prequal_id"}

    with get_conn() as conn:
        pq_row = fetchone(
            conn,
            "SELECT id, borrower_data FROM loan_prequals WHERE id = ?",
            (prequal_id,),
        )
    if not pq_row:
        return {"skipped": "prequal_not_found"}

    try:
        borrower = json.loads(pq_row.get("borrower_data") or "{}")
    except (TypeError, json.JSONDecodeError):
        borrower = {}

    borrower_email = (borrower.get("email") or "").strip()
    borrower_name = borrower.get("name") or "there"
    if not borrower_email:
        return {"skipped": "no_borrower_email"}

    from loan_officer.loan_app_invitation import send_loan_app_invitation
    result = send_loan_app_invitation(
        borrower_name=borrower_name,
        borrower_email=borrower_email,
        property_address=str(extracted_fields.get("property_address", "")),
        purchase_price=_coerce_money(extracted_fields.get("purchase_price")),
        closing_date=str(extracted_fields.get("closing_date", "")),
        correlation_id=doc_id,
    )

    if result.get("ok"):
        merged = dict(extra_blob) if isinstance(extra_blob, dict) else {}
        merged["_loan_app_invitation_sent"] = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            update(conn, "intake_documents", {
                "extracted_fields": json.dumps(merged),
                "updated_at":       datetime.now(timezone.utc).isoformat(),
            }, "doc_id = ?", (doc_id,))

    return result


def _coerce_money(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


_DATE_PATTERNS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y",
    "%d %B %Y", "%d %b %Y", "%Y/%m/%d",
]


def _normalize_date(value: Any) -> Optional[str]:
    """Coerce a wide variety of date strings into YYYY-MM-DD (deal-flow's PSATerms requirement)."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        for pat in _DATE_PATTERNS:
            try:
                return datetime.strptime(v, pat).date().isoformat()
            except ValueError:
                continue
    return None


# ── Inbound email-attachment webhook (Zapier-fired) ───────────────────────────

_ZAPIER_INBOUND_SECRET = os.environ.get("ZAPIER_INBOUND_SECRET", "")
_LETTER_ID_RE = re.compile(r"pql_[0-9a-f]+", re.IGNORECASE)


def _inbound_auth_ok() -> bool:
    """
    Authenticate the inbound webhook. Three accepted shapes:
      1. `Authorization: Bearer <ZAPIER_INBOUND_SECRET>` header
      2. `X-Zapier-Secret: <ZAPIER_INBOUND_SECRET>` header
      3. `?secret=<…>` query param (Zapier-friendly when configuring webhook URLs)
    Falls back to TRANCHI_API_SECRET if ZAPIER_INBOUND_SECRET isn't set —
    that way the endpoint isn't accidentally wide-open in dev.
    """
    expected = _ZAPIER_INBOUND_SECRET or os.environ.get("TRANCHI_API_SECRET", "")
    if not expected:
        return False
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth.split(" ", 1)[1], expected):
        return True
    if hmac.compare_digest(request.headers.get("X-Zapier-Secret", ""), expected):
        return True
    if hmac.compare_digest(request.args.get("secret", ""), expected):
        return True
    return False


def _find_latest_prequal_by_email(email: str) -> Optional[dict[str, Any]]:
    """Return the newest loan_prequals row whose borrower_data.email matches (case-insensitive)."""
    if not email:
        return None
    email_lc = email.lower()
    with get_conn() as conn:
        rows = fetchall(
            conn,
            "SELECT id, user_id, borrower_data, property_data, suggested_product, created_at "
            "FROM loan_prequals WHERE LOWER(borrower_data) LIKE ? "
            "ORDER BY created_at DESC LIMIT 10",
            (f'%"email": "{email_lc}"%',),  # match the JSON we serialize with json.dumps
        )
    for row in rows:
        try:
            borrower = json.loads(row.get("borrower_data") or "{}")
        except (TypeError, json.JSONDecodeError):
            borrower = {}
        if borrower.get("email", "").lower() == email_lc:
            try:
                prop = json.loads(row.get("property_data") or "{}")
            except (TypeError, json.JSONDecodeError):
                prop = {}
            return {
                "id":                row["id"],
                "user_id":           row["user_id"],
                "borrower_data":     borrower,
                "property_data":     prop,
                "suggested_product": row.get("suggested_product"),
            }
    return None


def _find_letter_id_in_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = _LETTER_ID_RE.search(text)
    return m.group(0) if m else None


def _find_prequal_by_letter_id(letter_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = fetchone(
            conn,
            "SELECT lp.id, lp.user_id, lp.borrower_data, lp.property_data, lp.suggested_product "
            "FROM prequal_letters pl "
            "JOIN loan_prequals lp ON lp.id = pl.prequal_id "
            "WHERE pl.letter_id = ?",
            (letter_id,),
        )
    if not row:
        return None
    try:
        borrower = json.loads(row.get("borrower_data") or "{}")
    except (TypeError, json.JSONDecodeError):
        borrower = {}
    try:
        prop = json.loads(row.get("property_data") or "{}")
    except (TypeError, json.JSONDecodeError):
        prop = {}
    return {
        "id":                row["id"],
        "user_id":           row["user_id"],
        "borrower_data":     borrower,
        "property_data":     prop,
        "suggested_product": row.get("suggested_product"),
    }


def _find_or_create_application(prequal_id: str, user_id: str) -> str:
    """Return an existing application_id for this prequal, or create one in APP_DOCS_PENDING."""
    with get_conn() as conn:
        row = fetchone(
            conn,
            "SELECT id FROM loan_applications WHERE prequal_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (prequal_id,),
        )
    if row:
        return row["id"]

    app_id = f"app_{uuid.uuid4().hex[:14]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        insert(conn, "loan_applications", {
            "id":                app_id,
            "prequal_id":        prequal_id,
            "user_id":           user_id or "",
            "status":            "APP_DOCS_PENDING",
            "current_state":     "APP_DOCS_PENDING",
            "lender_partner":    "",
            "lender_ref_id":     "",
            "docs_required":     "[]",
            "docs_received":     "[]",
            "underwriter_notes": "",
            "approved_amount":   None,
            "approved_rate":     None,
            "approved_term":     None,
            "conditions":        "[]",
            "audit_log":         json.dumps([{
                "event_type": "application_auto_created",
                "actor":      "system",
                "payload":    {"reason": "inbound-email-attachment"},
                "ts":         now,
            }]),
            "created_at":        now,
            "updated_at":        now,
        })
    return app_id


def _download_attachment(url: str, timeout: int = 30) -> tuple[bytes, str]:
    """Fetch the attachment bytes + content-type. Raises on transport/HTTP errors."""
    from shared.net import assert_safe_url
    assert_safe_url(url)  # SSRF guard — borrower/webhook-supplied URL
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    ct = resp.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip()
    return resp.content, ct


@intake_bp.route("/inbound-email-attachment", methods=["POST"])
def inbound_email_attachment():
    """
    Zapier-fired webhook: borrower (or anyone in their email thread) sent a doc
    as an attachment. Matches by sender email or letter_id reference, downloads
    the attachment to S3, classifies via Claude vision, and — if it's a
    purchase_contract — opens the transaction in deal-flow.

    Expected body (Zapier "Gmail: New Attachment" trigger fields):
      {
        "from_email": "borrower@gmail.com",
        "from_name":  "Borrower",
        "subject":    "Re: Your Pre-Qualification — Non-QM DSCR Loan",
        "message_id": "<gmail-msg-id>",
        "thread_id":  "...",
        "body_plain": "...",  # used to find a letter_id reference if present
        "attachment_filename":     "PSA_signed.pdf",
        "attachment_url":          "https://files.zapier.com/...",
        "attachment_content_type": "application/pdf",
        "received_at":             "2026-05-19T07:00:00Z"
      }

    Auth via Bearer/X-Zapier-Secret/?secret query matching ZAPIER_INBOUND_SECRET
    (falls back to TRANCHI_API_SECRET).
    """
    if not _inbound_auth_ok():
        return jsonify({"error": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    sender = (body.get("from_email") or body.get("sender_email") or "").strip().lower()
    subject = (body.get("subject") or "").strip()
    body_text = body.get("body_plain") or body.get("body") or ""
    message_id = (body.get("message_id") or body.get("gmail_message_id") or "").strip()
    filename = (body.get("attachment_filename") or body.get("filename") or "attachment.bin").strip()
    att_url = (body.get("attachment_url") or body.get("file_url") or body.get("url") or "").strip()
    att_ct = (body.get("attachment_content_type") or body.get("content_type") or "").strip() or None

    if not att_url:
        return jsonify({"error": "attachment_url is required"}), 400

    # Idempotency — short-circuit if we already processed this Gmail message
    if message_id:
        with get_conn() as conn:
            existing = fetchone(
                conn,
                "SELECT doc_id, status FROM intake_documents "
                "WHERE source_message_id = ? ORDER BY created_at DESC LIMIT 1",
                (message_id,),
            )
        if existing:
            return jsonify({
                "skipped":    "already_processed",
                "doc_id":     existing["doc_id"],
                "status":     existing["status"],
            }), 200

    # Match the borrower — letter_id in subject/body wins, then sender email
    letter_id = _find_letter_id_in_text(subject) or _find_letter_id_in_text(body_text)
    prequal = _find_prequal_by_letter_id(letter_id) if letter_id else None
    matched_via = "letter_id" if prequal else None
    if not prequal:
        prequal = _find_latest_prequal_by_email(sender)
        matched_via = "sender_email" if prequal else None
    if not prequal:
        return jsonify({
            "skipped":    "borrower_not_found",
            "sender":     sender,
            "letter_id":  letter_id,
        }), 200

    # Ensure we have an application to attach to
    app_id = _find_or_create_application(prequal["id"], prequal["user_id"] or "")

    # Download bytes from the Zapier URL
    try:
        data, fetched_ct = _download_attachment(att_url)
    except requests.RequestException as e:
        return jsonify({"error": f"attachment fetch failed: {e}"}), 502
    if not att_ct:
        att_ct = fetched_ct

    # Server-side upload via the existing presign + put_object_bytes flow
    s3 = get_default_client()
    if not s3.configured:
        return jsonify({
            "error": "AWS_S3_BUCKET is not set — cannot accept inbound attachments yet"
        }), 503

    try:
        presigned = presign_upload(
            deal_id=prequal["id"],
            filename=filename,
            content_type=att_ct,
            user_id=prequal["user_id"] or "",
            application_id=app_id,
            s3=s3,
        )
    except UploadError as e:
        return jsonify({"error": str(e)}), 400

    doc_id = presigned["doc_id"]
    s3_key = presigned["s3_key"]

    try:
        s3.put_object_bytes(s3_key=s3_key, data=data, content_type=att_ct)
    except Exception as e:
        return jsonify({"error": f"S3 put failed: {e}"}), 502

    # Stamp the message_id for dedup + flip status to 'uploaded'
    with get_conn() as conn:
        update(conn, "intake_documents", {
            "status":             "uploaded",
            "size_bytes":         len(data),
            "uploaded_at":        datetime.now(timezone.utc).isoformat(),
            "updated_at":         datetime.now(timezone.utc).isoformat(),
            "source":             "inbound_email",
            "source_message_id":  message_id or "",
        }, "doc_id = ?", (doc_id,))

    # Classify synchronously (re-using the existing route logic + auto-trigger)
    try:
        result = DocumentClassifier(s3=s3).classify(doc_id)
    except Exception as e:
        return jsonify({
            "ok":      True,
            "doc_id":  doc_id,
            "matched_via": matched_via,
            "prequal_id":  prequal["id"],
            "application_id": app_id,
            "classify_error": str(e),
        }), 200

    response: dict[str, Any] = {
        "ok":           True,
        "doc_id":       doc_id,
        "matched_via":  matched_via,
        "prequal_id":   prequal["id"],
        "application_id": app_id,
        "classification": result.to_dict(),
    }

    autofired = _maybe_autofire_prequal_letter(doc_id)
    if autofired:
        response["autofired_letter"] = autofired

    if result.doc_type == "purchase_contract":
        tx_result = _maybe_open_transaction(doc_id, result.extracted_fields)
        if tx_result:
            response["autofired_transaction"] = tx_result

        invite_result = _maybe_send_loan_app_invitation(doc_id, result.extracted_fields)
        if invite_result:
            response["autofired_loan_app_invitation"] = invite_result

    return jsonify(response), 200


@intake_bp.route("/applications/<app_id>/completeness", methods=["GET"])
@require_tranchi_auth
def completeness(app_id: str):
    """
    Compare uploaded + classified docs against the per-product checklist.
    `product` defaults to dscr (matches completeness._CHECKLISTS).
    """
    product = (request.args.get("product") or "dscr").lower()
    docs = list_docs_for_application(app_id)

    # We prefer the classifier's verdict; fall back to declared_doc_type.
    received = [
        (d.get("classified_doc_type") or d.get("declared_doc_type") or "").strip()
        for d in docs
        if d.get("status") in ("uploaded", "classified")
    ]
    received = [r for r in received if r]

    report = check_completeness(product, received)
    return jsonify({
        "application_id": app_id,
        "product": report.product,
        "received": report.received,
        "required_missing": report.required_missing,
        "optional_missing": report.optional_missing,
        "is_complete": report.is_complete,
        "completion_pct": report.completion_pct,
        "message": gap_message(report),
    })
