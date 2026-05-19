"""
loan_officer/intake/ocr_classifier.py — Claude vision wrapper for document type detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# TODO: import shared.llm.chat_with_vision once vision helper is added
# TODO: import shared.storage.s3_client for presigned GET URLs

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

# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass
class ClassificationResult:
    """Output of a single document classification pass."""

    doc_id: str
    doc_type: str                          # one of DOC_TYPES
    confidence: float = 0.0               # 0.0 – 1.0
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    # e.g. {"tax_year": 2023, "gross_income": 85000} for a W2
    warnings: list[str] = field(default_factory=list)
    raw_response: str = ""


# ── Classifier class ──────────────────────────────────────────────────────────


class DocumentClassifier:
    """
    Wraps Claude vision API to classify and extract fields from loan documents.

    Usage:
        classifier = DocumentClassifier()
        result = classifier.classify(doc_id="doc_xyz", s3_key="deals/.../doc.pdf")
    """

    _SYSTEM_PROMPT = (
        "You are a mortgage document classifier. Given a document image or PDF page, "
        "identify the document type from this list: "
        + ", ".join(DOC_TYPES)
        + ". Extract key financial fields. "
        "Return JSON only: "
        '{"doc_type": "...", "confidence": 0.95, "extracted_fields": {...}, "warnings": [...]}'
    )

    def classify(self, doc_id: str, s3_key: str) -> ClassificationResult:
        """
        Download doc from S3, send to Claude vision, return ClassificationResult.

        Steps:
          1. Generate presigned GET URL via s3_client.
          2. Call shared.llm.chat_with_vision with _SYSTEM_PROMPT + image/PDF.
          3. Parse JSON response into ClassificationResult.
          4. Persist result to loan_documents table (doc_type, extracted_fields).

        TODO: implement vision call and JSON parsing.
        """
        # TODO: s3 presign GET → base64 or URL → claude vision → parse JSON
        raise NotImplementedError("TODO: implement classify()")

    def classify_batch(self, items: list[dict[str, str]]) -> list[ClassificationResult]:
        """
        Classify multiple documents; items each have {doc_id, s3_key}.

        TODO: run classify() for each item; consider async/threadpool for speed.
        """
        # TODO: iterate or use ThreadPoolExecutor
        raise NotImplementedError("TODO: implement classify_batch()")

    def _parse_response(self, doc_id: str, raw: str) -> ClassificationResult:
        """
        Parse the raw JSON string from Claude into a ClassificationResult.

        TODO: json.loads, map fields, validate doc_type against DOC_TYPES.
        """
        # TODO: safe JSON parse with fallback to doc_type="other"
        raise NotImplementedError("TODO: implement _parse_response()")
