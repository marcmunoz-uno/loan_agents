"""
loan_officer/typeform/mapper.py — Typeform payload → BorrowerIntake dict.

The Typeform webhook payload shape (form_response.answers[]) is:
    {
      "field": {"id": "QbP0eSZ7YVxn", "ref": "8ae64c03-...", "type": "short_text"},
      "type": "text",
      "text": "Jane"
    }

Answer value lives under one of: text, email, phone_number, number, boolean,
date, choice.label, choices.labels[], file_url. We index answers by `field.ref`
(stable IDs assigned when the form was built) so renaming a question title or
re-ordering fields does not break the mapper.
"""

from __future__ import annotations

import re
from typing import Any


# ── Stable Typeform refs for prequal.typeform.com/qualification ──────────────
# Sourced by reading the live form HTML on 2026-05-21. If you re-publish the
# form and any ref changes, update here.

FORM_ID = "qualification"

# Contact-info subfields use `subfield_key` instead of ref in the answers payload.
CONTACT_INFO_REF = "7ab85967-fb0c-4b06-8e2c-bbc3672d8306"

FIELD_REFS = {
    # contact_info subfield keys
    "first_name":               ("contact_info", "first_name"),
    "last_name":                ("contact_info", "last_name"),
    "phone":                    ("contact_info", "phone_number"),
    "email":                    ("contact_info", "email"),
    "company":                  ("contact_info", "company"),

    # standalone fields keyed by ref
    "spoke_to_loan_officer":    ("ref", "c48f0785-021e-47b1-8cb3-e1abde5051a2"),
    "dob":                      ("ref", "552f63e5-191e-427a-ba6a-949d09ce4212"),
    "married":                  ("ref", "3c35cd7b-10f4-4b5a-80f9-73aca259ddd6"),
    "credit_score_estimate":    ("ref", "e1ab17f7-1633-4b1c-81db-9749de542afc"),
    "primary_residence_status": ("ref", "eca4023d-a5b0-43f9-96c0-ad743f00c2e9"),
    "primary_residence_years":  ("ref", "39b0cbcb-c681-4133-acaf-4a133abd2214"),
    "drivers_license_front_url":   ("ref", "473020ce-2d5e-4905-a9ac-19de3b955f10"),
    "drivers_license_back_url":    ("ref", "fedb915e-5d04-4062-9ab2-44c7e711f94d"),
    "proof_of_residence_url":      ("ref", "bff2e4a9-172a-4e15-a32b-c6bd48bb6f73"),
    "articles_of_org_url":         ("ref", "7f7af193-2645-44cf-99ae-787efb890552"),
    "operating_agreement_url":     ("ref", "9d16d5a8-5204-429f-8f6a-3e2b4fb28f16"),
    "ein_document_url":            ("ref", "30905376-2b81-48cc-8dc3-50027c8a15c7"),
    "asset_statement_recent_url":  ("ref", "6243a449-2eb1-440a-b065-8754b42eecd2"),
    "asset_statement_previous_url":("ref", "aa66a263-9943-4815-922e-1b629d0725ce"),
    "asset_statement_extra_url":   ("ref", "140943dc-f985-4bcb-bb7c-b42150b14daf"),
    "credit_pull_authorized":      ("ref", "ab328aab-08dc-4782-b4c3-36a51b8aa50d"),
}

# Normalised choice labels for the LO routing field.
_LO_CHOICE_NORMALISE = {
    "yazan": "yazan", "austin": "austin", "jeff": "jeff",
    "joseph": "joseph", "none": "none",
}

_RESIDENCE_NORMALISE = {
    "own": "own",
    "rent": "rent",
    "living rent free": "living_rent_free",
}


def _extract_answer_value(answer: dict[str, Any]) -> Any:
    """Pull the value out of a Typeform answer regardless of its `type`."""
    t = answer.get("type")
    if t == "text":            return answer.get("text", "")
    if t == "email":           return answer.get("email", "")
    if t == "phone_number":    return answer.get("phone_number", "")
    if t == "number":          return answer.get("number")
    if t == "boolean":         return answer.get("boolean")
    if t == "date":            return answer.get("date", "")
    if t == "url":             return answer.get("url", "")
    if t == "file_url":        return answer.get("file_url", "")
    if t == "choice":          return (answer.get("choice") or {}).get("label", "")
    if t == "choices":         return (answer.get("choices") or {}).get("labels", []) or []
    return answer.get("text") or answer.get("value") or ""


def _index_answers(answers: list[dict[str, Any]]) -> dict[tuple[str, str], Any]:
    """
    Build a lookup keyed by ('ref', <ref>) for standalone answers and
    ('contact_info', <subfield_key>) for contact-info subanswers.
    """
    out: dict[tuple[str, str], Any] = {}
    for a in answers or []:
        field = a.get("field") or {}
        ref = field.get("ref", "")
        ftype = field.get("type", "")

        # Contact-info answers are a single nested object whose ref equals
        # CONTACT_INFO_REF and whose value is at answer["contact_info"][subfield].
        if ftype == "contact_info" and ref == CONTACT_INFO_REF:
            ci = a.get("contact_info") or {}
            for sub_key, sub_val in ci.items():
                out[("contact_info", sub_key)] = sub_val
            continue

        out[("ref", ref)] = _extract_answer_value(a)
    return out


def _parse_credit_score(raw: Any) -> int | None:
    """Borrower-typed credit score is a short_text — extract the first 3-digit number."""
    if raw is None:
        return None
    s = str(raw)
    m = re.search(r"\b(\d{3})\b", s)
    if not m:
        return None
    n = int(m.group(1))
    return n if 300 <= n <= 850 else None


def _parse_years(raw: Any) -> float | None:
    """Years-owned is short_text — pull the first number."""
    if raw is None:
        return None
    m = re.search(r"\d+(\.\d+)?", str(raw))
    return float(m.group(0)) if m else None


def map_payload(form_response: dict[str, Any]) -> dict[str, Any]:
    """
    Given a Typeform `form_response` object (the payload Typeform POSTs to the
    webhook URL under the key 'form_response'), return a dict shaped for the
    loan_borrower_intakes table — minus the system columns added by the route
    handler (intake_id, received_at, raw_payload, ...).
    """
    answers = form_response.get("answers") or []
    idx = _index_answers(answers)

    def g(key: str) -> Any:
        spec = FIELD_REFS.get(key)
        if not spec:
            return None
        return idx.get(spec)

    married_label = (g("married") or "").lower() if isinstance(g("married"), str) else ""
    married = 1 if married_label == "yes" else (0 if married_label == "no" else None)

    residence_label = (g("primary_residence_status") or "").lower() if isinstance(g("primary_residence_status"), str) else ""
    primary_residence_status = _RESIDENCE_NORMALISE.get(residence_label, "")

    lo_label = (g("spoke_to_loan_officer") or "").lower() if isinstance(g("spoke_to_loan_officer"), str) else ""
    spoke_to_lo = _LO_CHOICE_NORMALISE.get(lo_label, "")

    # The credit-pull checkbox returns a list of selected labels; presence == authorised
    pull_choices = g("credit_pull_authorized")
    credit_pull_authorized = 1 if (isinstance(pull_choices, list) and len(pull_choices) > 0) else 0

    return {
        "typeform_response_id": form_response.get("token", ""),
        "typeform_form_id":     form_response.get("form_id", FORM_ID),
        "submitted_at":         form_response.get("submitted_at", ""),

        "first_name":           g("first_name") or "",
        "last_name":            g("last_name") or "",
        "email":                g("email") or "",
        "phone":                g("phone") or "",
        "company":              g("company") or "",

        "dob":                      g("dob") or "",
        "married":                  married,
        "credit_score_estimate":    _parse_credit_score(g("credit_score_estimate")),
        "primary_residence_status": primary_residence_status,
        "primary_residence_years":  _parse_years(g("primary_residence_years")),
        "spoke_to_loan_officer":    spoke_to_lo,

        "drivers_license_front_url":    g("drivers_license_front_url") or "",
        "drivers_license_back_url":     g("drivers_license_back_url") or "",
        "proof_of_residence_url":       g("proof_of_residence_url") or "",
        "articles_of_org_url":          g("articles_of_org_url") or "",
        "operating_agreement_url":      g("operating_agreement_url") or "",
        "ein_document_url":             g("ein_document_url") or "",
        "asset_statement_recent_url":   g("asset_statement_recent_url") or "",
        "asset_statement_previous_url": g("asset_statement_previous_url") or "",
        "asset_statement_extra_url":    g("asset_statement_extra_url") or "",

        "credit_pull_authorized":   credit_pull_authorized,
    }
