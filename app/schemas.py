"""Typed contracts between agents. Pydantic validation is itself a guardrail:
any LLM output that does not match these schemas is rejected and retried."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class LineItem(BaseModel):
    sku: str = Field(min_length=1)
    description: str = ""
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    amount: float = Field(ge=0)


class Invoice(BaseModel):
    invoice_number: str = Field(min_length=1)
    invoice_date: Optional[str] = None
    vendor_name: str = Field(min_length=1)
    vendor_tax_id: Optional[str] = None
    po_number: Optional[str] = None
    bank_account: Optional[str] = None
    currency: str = "USD"
    line_items: list[LineItem] = Field(min_length=1)
    subtotal: float = Field(ge=0)
    tax: float = Field(ge=0, default=0)
    total: float = Field(ge=0)
    notes: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def upper_currency(cls, v: str) -> str:
        return v.strip().upper()


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class Issue(BaseModel):
    code: str
    severity: Severity
    message: str
    agent: str


class Decision(str, Enum):
    auto_approved = "auto_approved"
    human_review = "human_review"
    rejected = "rejected"
    approved = "approved"  # approved by a human


class RiskAssessment(BaseModel):
    risk_score: int = Field(ge=0, le=100)
    summary: str
    recommended_action: str  # approve | review | reject  (advisory only)
