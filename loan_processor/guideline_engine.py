"""
loan_processor/guideline_engine.py — Loads + queries lender guidelines.

The guidelines_index.json is the fast-path for lender matching. The full
markdown files are loaded on demand and passed to the LLM as context.

Usage:
    from loan_processor.guideline_engine import GuidelineEngine
    engine = GuidelineEngine()
    matches = engine.match_lenders(fico=720, dscr=1.18, ltv=0.75, product_type="dscr", state="MI")
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

GUIDELINES_DIR = Path(__file__).parent.parent / "lender_guidelines"
INDEX_PATH = GUIDELINES_DIR / "guidelines_index.json"


class GuidelineEngine:
    """Loads and queries the lender guidelines matrix."""

    def __init__(self, guidelines_dir: Optional[Path] = None) -> None:
        self._dir = guidelines_dir or GUIDELINES_DIR
        self._index: dict[str, dict[str, Any]] = {}
        self._docs_cache: dict[str, str] = {}
        self._load_index()

    # ─────────────────────────────────────────────────────────────────────────
    # Index loading
    # ─────────────────────────────────────────────────────────────────────────

    def _load_index(self) -> None:
        idx_path = self._dir / "guidelines_index.json"
        if not idx_path.exists():
            logger.error("[guideline_engine] guidelines_index.json not found at %s", idx_path)
            return
        with open(idx_path) as f:
            self._index = json.load(f)
        logger.info("[guideline_engine] Loaded %d lender entries", len(self._index))

    def reload(self) -> None:
        """Force reload from disk (useful after guidelines update)."""
        self._docs_cache.clear()
        self._load_index()

    def get_index(self) -> dict[str, Any]:
        """Return the full index dict."""
        return dict(self._index)

    # ─────────────────────────────────────────────────────────────────────────
    # Full guideline doc loader
    # ─────────────────────────────────────────────────────────────────────────

    def get_guideline_doc(self, lender_id: str) -> Optional[str]:
        """Return full markdown text for a lender. Cached after first load."""
        if lender_id in self._docs_cache:
            return self._docs_cache[lender_id]
        entry = self._index.get(lender_id)
        if not entry:
            return None
        doc_file = entry.get("guidelines_doc", "")
        doc_path = self._dir / doc_file
        if not doc_path.exists():
            logger.warning("[guideline_engine] Doc file not found: %s", doc_path)
            return None
        text = doc_path.read_text()
        self._docs_cache[lender_id] = text
        return text

    # ─────────────────────────────────────────────────────────────────────────
    # Lender matching
    # ─────────────────────────────────────────────────────────────────────────

    def match_lenders(
        self,
        fico: int,
        ltv: float,
        product_type: str,
        dscr: Optional[float] = None,
        state: str = "",
        loan_amount: float = 0,
        property_type: str = "SFR",
        loan_purpose: str = "purchase",
    ) -> list[dict[str, Any]]:
        """
        Return a ranked list of lenders compatible with the given deal parameters.

        Each result includes:
            lender_id, lender, product, fit_score (0-100), reasons_qualify,
            reasons_decline, guideline_entry
        """
        results = []
        # Normalize inputs
        prop_type_norm = _normalize_property_type(property_type)
        state_upper = state.upper()
        ltv_key = "max_ltv_cashout" if loan_purpose == "cash_out" else "max_ltv_purchase"

        for lender_id, g in self._index.items():
            if g.get("product_type") != product_type:
                continue

            decline_reasons: list[str] = []
            qualify_reasons: list[str] = []

            # FICO check
            min_fico = g.get("min_fico", 0)
            if fico < min_fico:
                decline_reasons.append(f"FICO {fico} below min {min_fico}")
            else:
                qualify_reasons.append(f"FICO {fico} meets min {min_fico}")

            # LTV check
            max_ltv = g.get(ltv_key) or g.get("max_ltv_purchase", 1.0)
            if ltv > max_ltv:
                decline_reasons.append(f"LTV {ltv:.0%} exceeds max {max_ltv:.0%}")
            else:
                qualify_reasons.append(f"LTV {ltv:.0%} within {max_ltv:.0%} max")

            # DSCR check (only for dscr/brrrr products)
            if product_type in ("dscr", "brrrr") and dscr is not None:
                min_dscr = g.get("min_dscr", 0)
                no_dscr_ok = g.get("no_dscr_variant", False)
                if dscr < min_dscr:
                    if no_dscr_ok and ltv <= 0.75:
                        qualify_reasons.append(
                            f"DSCR {dscr:.2f} below {min_dscr} but no-DSCR variant available (+50–75bps)"
                        )
                    else:
                        decline_reasons.append(
                            f"DSCR {dscr:.2f} below min {min_dscr}"
                        )
                else:
                    qualify_reasons.append(f"DSCR {dscr:.2f} meets min {min_dscr}")

            # Loan amount check
            min_loan = g.get("min_loan_amount", 0)
            max_loan = g.get("max_loan_amount", 999_999_999)
            if loan_amount > 0:
                if loan_amount < min_loan:
                    decline_reasons.append(f"Loan ${loan_amount:,.0f} below min ${min_loan:,.0f}")
                elif loan_amount > max_loan:
                    decline_reasons.append(f"Loan ${loan_amount:,.0f} exceeds max ${max_loan:,.0f}")
                else:
                    qualify_reasons.append(f"Loan amount ${loan_amount:,.0f} within program limits")

            # Property type check
            eligible_types = g.get("property_types", [])
            if eligible_types and prop_type_norm not in eligible_types:
                decline_reasons.append(
                    f"Property type '{prop_type_norm}' not in eligible types: {eligible_types}"
                )
            elif eligible_types:
                qualify_reasons.append(f"Property type '{prop_type_norm}' is eligible")

            # State check
            eligible_states = g.get("states", ["all"])
            if state_upper and "all" not in eligible_states:
                if state_upper not in [s.upper() for s in eligible_states]:
                    decline_reasons.append(
                        f"State '{state_upper}' not in eligible states"
                    )
                else:
                    qualify_reasons.append(f"State '{state_upper}' is eligible")

            # Compute fit score
            total = len(qualify_reasons) + len(decline_reasons)
            fit_score = int((len(qualify_reasons) / total) * 100) if total > 0 else 50

            # Hard decline if any FICO or LTV issue
            hard_decline = any("FICO" in r or "LTV" in r or "State" in r for r in decline_reasons)
            if hard_decline:
                fit_score = min(fit_score, 30)

            results.append({
                "lender_id": lender_id,
                "lender": g.get("lender", ""),
                "product": g.get("product", ""),
                "product_type": product_type,
                "fit_score": fit_score,
                "qualifies": len(decline_reasons) == 0,
                "decline_reasons": decline_reasons,
                "qualify_reasons": qualify_reasons,
                "hot_buttons": g.get("hot_buttons", []),
                "rate_range_pct": g.get("rate_range_pct", []),
                "min_reserves_months": g.get("min_reserves_months", 0),
                "submission_email": g.get("submission_email", ""),
                "guideline_entry": g,
            })

        # Sort: qualifying first, then by fit_score descending
        results.sort(key=lambda r: (0 if r["qualifies"] else 1, -r["fit_score"]))
        return results

    def get_all_lender_ids(self) -> list[str]:
        return list(self._index.keys())


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_property_type(raw: str) -> str:
    """Map our internal property_type to the lender's property_types list format."""
    mapping = {
        "single_family":     "SFR",
        "sfr":               "SFR",
        "multi_family_2_4":  "2-4 unit",
        "2_4_unit":          "2-4 unit",
        "multifamily_5plus": "5-8 unit",
        "condo":             "condo_warrantable",
        "townhouse":         "townhouse",
        "commercial":        "commercial",
    }
    return mapping.get(raw.lower(), raw)


# Singleton for module-level use
_engine: Optional[GuidelineEngine] = None


def get_engine() -> GuidelineEngine:
    global _engine
    if _engine is None:
        _engine = GuidelineEngine()
    return _engine
