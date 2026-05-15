"""
shared/schemas.py — Pydantic models shared across loan_officer and tx_coordinator.
"""

from __future__ import annotations
from datetime import date
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ── Shared primitives ─────────────────────────────────────────────────────────

class Address(BaseModel):
    street: str
    city: str
    state: str
    zip_code: str = ""

    def __str__(self) -> str:
        return f"{self.street}, {self.city}, {self.state} {self.zip_code}".strip()


class ContactInfo(BaseModel):
    name: str
    email: str = ""
    phone: str = ""


# ── Loan Officer schemas ──────────────────────────────────────────────────────

class BorrowerProfile(BaseModel):
    user_id: str
    name: str
    email: str = ""
    phone: str = ""
    # Financial profile
    annual_income: float = 0.0         # gross annual income
    credit_score: Optional[int] = None  # 300-850
    properties_owned: int = 0
    liquidity: float = 0.0             # verifiable liquid assets
    # Loan intent
    loan_purpose: Literal[
        "purchase", "refinance", "cash_out_refi", "construction"
    ] = "purchase"
    desired_loan_amount: float = 0.0
    down_payment: float = 0.0
    down_payment_pct: float = 0.0      # computed if 0


class PropertyProfile(BaseModel):
    address: str
    property_type: Literal[
        "single_family", "multi_family_2_4", "multifamily_5plus",
        "condo", "townhouse", "commercial", "land", "mixed_use"
    ] = "single_family"
    purchase_price: float = 0.0
    estimated_value: float = 0.0
    monthly_rent: float = 0.0          # actual or projected
    annual_taxes: float = 0.0
    annual_insurance: float = 0.0
    hoa_monthly: float = 0.0
    condition: Literal["excellent", "good", "fair", "poor"] = "good"
    rehab_budget: float = 0.0          # fix & flip only
    arv: float = 0.0                   # after-repair value


class PrequalRequest(BaseModel):
    borrower: BorrowerProfile
    property: PropertyProfile
    desired_product: Optional[str] = None  # optional hint; lender_router overrides
    notes: str = ""


class PrequalResponse(BaseModel):
    prequal_id: str
    status: str
    score: float                       # 0-100
    suggested_product: str
    monthly_payment_estimate: float
    dscr: Optional[float] = None
    ltv: Optional[float] = None
    strengths: list[str] = []
    concerns: list[str] = []
    next_steps: list[str] = []
    created_at: str


class LoanApplicationRequest(BaseModel):
    prequal_id: str
    borrower: BorrowerProfile
    property: PropertyProfile
    target_close_date: Optional[str] = None
    notes: str = ""


# ── Transaction Coordinator schemas ──────────────────────────────────────────

class PSATerms(BaseModel):
    purchase_price: float
    earnest_money: float = 0.0
    closing_date: str                  # ISO date string: YYYY-MM-DD
    inspection_period_days: int = 10
    financing_contingency_days: int = 21
    title_contingency_days: int = 14
    seller_concessions: float = 0.0
    # Parties
    buyer_name: str
    buyer_email: str = ""
    buyer_phone: str = ""
    seller_name: str
    seller_email: str = ""
    seller_phone: str = ""
    buyer_agent_name: str = ""
    listing_agent_name: str = ""
    # Property
    property_address: str
    # Misc
    notes: str = ""

    @field_validator("closing_date")
    @classmethod
    def validate_closing_date(cls, v: str) -> str:
        date.fromisoformat(v)  # raises ValueError if invalid
        return v


class PartyType(BaseModel):
    party_type: Literal[
        "buyer", "seller", "buyer_agent", "listing_agent",
        "title", "escrow", "inspector", "lender", "insurance", "other"
    ]
    name: str
    email: str = ""
    phone: str = ""
    company: str = ""
    notes: str = ""


class MilestoneUpdate(BaseModel):
    notes: str = ""
    completed_at: Optional[str] = None  # ISO datetime; defaults to now


class DocumentRef(BaseModel):
    doc_type: str
    s3_url: str
    party_uploaded: str = ""
    notes: str = ""


class CommunicationLog(BaseModel):
    party_id: Optional[int] = None
    direction: Literal["in", "out"] = "out"
    channel: Literal["email", "sms", "imessage", "call", "in_person", "portal"] = "email"
    summary: str
    full_text: str = ""
    occurred_at: Optional[str] = None  # ISO datetime; defaults to now


class ChatMessage(BaseModel):
    user_id: str
    message: str
    context: dict[str, Any] = {}


# ── Loan Processor schemas ────────────────────────────────────────────────────

class Condition(BaseModel):
    """An underwriting condition — doc request, clarification, escrow holdback, or repair."""
    condition_type: Literal[
        "doc_request", "clarification", "escrow_holdback", "repair_required"
    ] = "doc_request"
    severity: Literal[
        "prior_to_submission", "prior_to_close", "funding"
    ] = "prior_to_close"
    description: str
    lender_specific: str = ""   # lender_id or "all"
    required: bool = True


class RedFlag(BaseModel):
    """A pre-underwriting red flag that will cause delay, re-trade, or decline."""
    flag_type: Literal[
        "fico_below_min", "ltv_above_max", "property_ineligible",
        "dscr_too_low", "reserves_short", "cashflow_negative",
    ]
    severity: Literal["deal_killer", "significant", "minor"]
    description: str
    mitigation_suggestion: str = ""


class PreUnderwritingReportSchema(BaseModel):
    """Pydantic schema for API request/response validation of pre-UW reports."""
    application_id: str
    summary: str
    overall_status: Literal["clean", "conditional", "decline_risk"]
    lender_fit: list[dict[str, Any]] = []
    conditions: list[Condition] = []
    red_flags: list[RedFlag] = []
    computed_metrics: dict[str, Any] = {}
    suggested_lender: str = ""
    credit_memo_draft: str = ""
    generated_at: str
