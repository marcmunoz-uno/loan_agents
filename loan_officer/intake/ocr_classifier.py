"""
loan_officer/intake/ocr_classifier.py — Claude vision document classifier.

One call per document: Claude sees the page(s) and returns
    {"doc_type": "...", "confidence": 0.9, "extracted_fields": {...}, "warnings": [...]}

The result is persisted to intake_documents (classified_doc_type,
extracted_fields, confidence, warnings, status='classified').
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from shared.db import get_conn, fetchone, update
from shared.llm import chat_with_vision
from shared.s3_client import S3Client, S3NotConfigured, get_default_client


# ── Document type taxonomy ────────────────────────────────────────────────────

DOC_TYPES = [
    "w2",
    "bank_stmt",
    "rent_roll",
    "tax_return",
    "profit_loss",
    "purchase_contract",
    "appraisal",
    "title_report",
    "insurance_binder",
    "entity_docs",
    "id_government",
    "payoff_statement",
    "rehab_scope",
    "lease_agreement",
    "other",
]

_SUPPORTED_MEDIA = {
    "application/pdf",
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
}


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    """Output of a single document classification pass."""
    doc_id: str
    doc_type: str
    confidence: float = 0.0
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raw_response: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "doc_type": self.doc_type,
            "confidence": self.confidence,
            "extracted_fields": dict(self.extracted_fields),
            "warnings": list(self.warnings),
        }


# ── Errors ────────────────────────────────────────────────────────────────────

class ClassifierError(RuntimeError):
    """Recoverable classifier error (unsupported media, missing doc, etc.)."""


# ── Classifier ────────────────────────────────────────────────────────────────

class DocumentClassifier:
    """Wraps Claude vision to classify intake documents."""

    _SYSTEM_PROMPT = (
        "You are a mortgage document classifier. "
        "Given a document image or PDF, identify the document type from this exact list:\n"
        + "\n".join(f"  - {t}" for t in DOC_TYPES)
        + "\n\nExtract the most important fields for that document type:\n"
        "  - w2:                 employer_name, tax_year, gross_wages, federal_tax_withheld\n"
        "  - bank_stmt:          bank_name, account_holder, statement_period, ending_balance\n"
        "  - tax_return:         tax_year, filer_name, agi, total_income\n"
        "  - purchase_contract:  buyer_name, seller_name, property_address, purchase_price, closing_date\n"
        "  - appraisal:          property_address, appraised_value, effective_date\n"
        "  - lease_agreement:    property_address, tenant_name, monthly_rent, lease_term_months\n"
        "  - rent_roll:          property_address, unit_count, total_monthly_rent\n"
        "  - id_government:      full_name, dob, id_type, id_number_masked, expiration_date\n"
        "  - other doc types:    extract any obvious key/value fields\n\n"
        "Set warnings if the document is unclear, low-quality, partially obscured, or appears "
        "to be a different type than the field set suggests.\n\n"
        "Reply with a single JSON object — no prose, no markdown fences:\n"
        '{"doc_type": "<one of the list>", "confidence": <0.0-1.0>, '
        '"extracted_fields": {...}, "warnings": [...]}'
    )

    def __init__(self, s3: Optional[S3Client] = None):
        self.s3 = s3 or get_default_client()

    # ── Public ────────────────────────────────────────────────────────────────

    def classify(self, doc_id: str) -> ClassificationResult:
        """
        Fetch the doc bytes from S3, send to Claude vision, persist + return the result.

        Raises:
            ClassifierError    if the doc row is missing, not yet uploaded, or media unsupported
            S3NotConfigured    if the S3 client has no bucket
        """
        row = _fetch_row(doc_id)
        if row is None:
            raise ClassifierError(f"unknown doc_id: {doc_id!r}")
        if row["status"] not in ("uploaded", "classifying"):
            raise ClassifierError(
                f"doc {doc_id} not ready for classification (status={row['status']!r})"
            )
        media_type = row["content_type"]
        if media_type not in _SUPPORTED_MEDIA:
            self._persist_failure(doc_id, f"unsupported content_type: {media_type}")
            raise ClassifierError(f"unsupported content_type: {media_type!r}")

        _mark_classifying(doc_id)

        try:
            data = self.s3.get_object_bytes(row["s3_key"])
        except Exception as e:
            self._persist_failure(doc_id, f"S3 fetch failed: {e}")
            raise

        try:
            raw = chat_with_vision(
                prompt="Classify and extract fields. JSON only.",
                media=[{"data": data, "media_type": media_type}],
                system=self._SYSTEM_PROMPT,
                model_tier="standard",
                max_tokens=1024,
            )
        except Exception as e:
            self._persist_failure(doc_id, f"vision call failed: {e}")
            raise

        result = self._parse_response(doc_id, raw)
        self._persist_success(doc_id, result)
        return result

    def classify_batch(self, doc_ids: list[str]) -> list[ClassificationResult]:
        """
        Run classify() for each doc sequentially. Failures are caught per-doc and
        surfaced as a ClassificationResult with doc_type='other' + warnings.

        For parallelism: wrap in ThreadPoolExecutor at the caller — the Anthropic
        client is thread-safe and IO-bound.
        """
        results: list[ClassificationResult] = []
        for did in doc_ids:
            try:
                results.append(self.classify(did))
            except Exception as e:
                results.append(ClassificationResult(
                    doc_id=did,
                    doc_type="other",
                    confidence=0.0,
                    warnings=[f"classify failed: {e}"],
                ))
        return results

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_response(self, doc_id: str, raw: str) -> ClassificationResult:
        """Best-effort JSON extraction with safe fallbacks."""
        text = raw.strip()
        # Claude sometimes wraps JSON in ```json fences despite instructions
        text = _strip_fences(text)
        # If the model returned a leading explanation, grab the outermost {...}
        match = re.search(r"\{.*\}", text, re.DOTALL)
        payload: dict[str, Any] = {}
        if match:
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError:
                payload = {}

        doc_type = str(payload.get("doc_type", "other"))
        if doc_type not in DOC_TYPES:
            doc_type = "other"

        try:
            confidence = float(payload.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        extracted = payload.get("extracted_fields") or {}
        if not isinstance(extracted, dict):
            extracted = {}

        warnings = payload.get("warnings") or []
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        warnings = [str(w) for w in warnings]

        if not payload:
            warnings.insert(0, "classifier output was not valid JSON; falling back to 'other'")

        return ClassificationResult(
            doc_id=doc_id,
            doc_type=doc_type,
            confidence=confidence,
            extracted_fields=extracted,
            warnings=warnings,
            raw_response=raw,
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist_success(self, doc_id: str, result: ClassificationResult) -> None:
        now = _now()
        with get_conn() as conn:
            update(conn, "intake_documents", {
                "classified_doc_type": result.doc_type,
                "confidence": result.confidence,
                "extracted_fields": json.dumps(result.extracted_fields),
                "warnings": json.dumps(result.warnings),
                "status": "classified",
                "error_message": "",
                "classified_at": now,
                "updated_at": now,
            }, "doc_id = ?", (doc_id,))

    def _persist_failure(self, doc_id: str, message: str) -> None:
        with get_conn() as conn:
            update(conn, "intake_documents", {
                "status": "failed",
                "error_message": message[:500],
                "updated_at": _now(),
            }, "doc_id = ?", (doc_id,))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_row(doc_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = fetchone(conn, "SELECT * FROM intake_documents WHERE doc_id = ?", (doc_id,))
    return dict(row) if row else None


def _mark_classifying(doc_id: str) -> None:
    with get_conn() as conn:
        update(conn, "intake_documents", {
            "status": "classifying",
            "updated_at": _now(),
        }, "doc_id = ?", (doc_id,))


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip()).strip()
