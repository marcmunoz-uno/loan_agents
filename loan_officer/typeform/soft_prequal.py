"""
loan_officer/typeform/soft_prequal.py — Borrower-only soft prequal scoring.

Unlike loan_officer.prequal.score_prequal (which needs property data), this
runs on the limited borrower-side info collected by the Typeform:
credit score estimate, identity docs, asset statements, credit-pull auth.

Returns: status ("pass" | "conditional" | "decline"), score 0-100, list of
missing required docs, list of human-readable decision reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


SoftStatus = Literal["pass", "conditional", "decline"]


# Required for a soft prequal to be even considered.
REQUIRED_DOCS = {
    "drivers_license_front_url":   "Driver's License (Front)",
    "drivers_license_back_url":    "Driver's License (Back)",
    "proof_of_residence_url":      "Proof of Primary Residence",
    "asset_statement_recent_url":  "Recent 30 days of Asset Statements",
    "asset_statement_previous_url":"Previous 30 days of Asset Statements",
}

# Nice-to-have but not required to score.
OPTIONAL_ENTITY_DOCS = {
    "articles_of_org_url":     "Articles of Organization",
    "operating_agreement_url": "Operating Agreement",
    "ein_document_url":        "EIN Document",
}


@dataclass
class SoftPrequalResult:
    status: SoftStatus
    score: int
    missing_required_docs: list[str] = field(default_factory=list)
    decision_reasons: list[str] = field(default_factory=list)


def score(intake: dict) -> SoftPrequalResult:
    """
    Score a mapped intake dict (see mapper.map_payload).

    Gates (any failing → decline):
      - Credit-pull authorization NOT given
      - Credit score < 620

    Scoring once gates pass (0–100):
      - Credit score:  ≥760 +40, ≥720 +35, ≥680 +28, ≥640 +18, ≥620 +10
      - Required docs: +10 per doc, max +50
      - Entity docs:   +5 per doc (capped +10 — only one entity needed)

    Status thresholds:
      - score ≥ 80 → pass
      - score ≥ 60 → conditional
      - else        → decline
    """
    reasons: list[str] = []

    # ── Gates ────────────────────────────────────────────────────────────────
    if not intake.get("credit_pull_authorized"):
        reasons.append("Credit pull authorization not provided.")
        return SoftPrequalResult(
            status="decline", score=0,
            missing_required_docs=_missing_required(intake),
            decision_reasons=reasons,
        )

    credit = intake.get("credit_score_estimate")
    if credit is None:
        reasons.append("Credit score estimate could not be parsed from the form.")
        return SoftPrequalResult(
            status="decline", score=0,
            missing_required_docs=_missing_required(intake),
            decision_reasons=reasons,
        )
    if credit < 620:
        reasons.append(f"Estimated credit score ({credit}) is below the 620 minimum for DSCR financing.")
        return SoftPrequalResult(
            status="decline", score=0,
            missing_required_docs=_missing_required(intake),
            decision_reasons=reasons,
        )

    # ── Scoring ──────────────────────────────────────────────────────────────
    pts = 0

    if credit >= 760:
        pts += 40
        reasons.append(f"Excellent estimated credit ({credit}) — qualifies for best DSCR rates.")
    elif credit >= 720:
        pts += 35
        reasons.append(f"Strong estimated credit ({credit}).")
    elif credit >= 680:
        pts += 28
        reasons.append(f"Solid estimated credit ({credit}).")
    elif credit >= 640:
        pts += 18
        reasons.append(f"Moderate estimated credit ({credit}) — limits product options.")
    else:
        pts += 10
        reasons.append(f"Low-end credit ({credit}) — DSCR options restricted.")

    missing_required: list[str] = []
    required_present = 0
    for key, label in REQUIRED_DOCS.items():
        if intake.get(key):
            required_present += 1
        else:
            missing_required.append(label)
    pts += min(required_present * 10, 50)

    entity_present = sum(1 for key in OPTIONAL_ENTITY_DOCS if intake.get(key))
    pts += min(entity_present * 5, 10)
    if entity_present == 0:
        reasons.append("No entity docs (LLC / Operating Agreement / EIN) uploaded — fine for individuals but required if borrowing in an LLC.")

    if missing_required:
        reasons.append(
            "Missing required docs: " + ", ".join(missing_required) + "."
        )

    # ── Status ───────────────────────────────────────────────────────────────
    if pts >= 80:
        status: SoftStatus = "pass"
    elif pts >= 60:
        status = "conditional"
    else:
        status = "decline"

    return SoftPrequalResult(
        status=status,
        score=pts,
        missing_required_docs=missing_required,
        decision_reasons=reasons,
    )


def _missing_required(intake: dict) -> list[str]:
    return [label for key, label in REQUIRED_DOCS.items() if not intake.get(key)]
