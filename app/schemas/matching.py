"""Result of matching an invoice to a purchase order."""

from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.schemas.enums import POStatus


class POMatchStatus(StrEnum):
    MATCHED = "MATCHED"  # exactly one PO found and every check passed
    CHECKS_FAILED = "CHECKS_FAILED"  # exactly one PO found, but status/vendor/currency failed
    NOT_FOUND = "NOT_FOUND"  # no PO has this reference
    AMBIGUOUS = "AMBIGUOUS"  # several POs share the normalized reference; none is chosen
    NO_PO_REFERENCE = "NO_PO_REFERENCE"  # the invoice has no usable PO reference


class MatchReasonCode(StrEnum):
    PO_FOUND = "PO_FOUND"
    MISSING_PO_REFERENCE = "MISSING_PO_REFERENCE"
    PO_NOT_FOUND = "PO_NOT_FOUND"
    AMBIGUOUS_PO = "AMBIGUOUS_PO"
    PO_APPROVED = "PO_APPROVED"
    PO_CLOSED = "PO_CLOSED"
    VENDOR_MATCHED = "VENDOR_MATCHED"
    VENDOR_MATCHED_IGNORING_SUFFIX = "VENDOR_MATCHED_IGNORING_SUFFIX"
    VENDOR_MISMATCH = "VENDOR_MISMATCH"
    VENDOR_MISSING = "VENDOR_MISSING"
    CURRENCY_MATCHED = "CURRENCY_MATCHED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    CURRENCY_MISSING = "CURRENCY_MISSING"


class Severity(StrEnum):
    INFO = "info"  # a check passed
    WARNING = "warning"  # passed, but a reviewer may want to look
    ERROR = "error"  # failed; the invoice cannot be matched as is


class MatchReason(BaseModel):
    code: MatchReasonCode
    severity: Severity
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class MatchedPO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None
    po_number: str
    vendor_id: int | None
    vendor_name: str
    currency: str
    total_amount: Decimal
    status: POStatus


class POMatchResult(BaseModel):
    status: POMatchStatus
    po_reference: str | None = Field(description="The invoice's PO reference, normalized")
    po_key: str | None = Field(description="Comparison key used for the lookup")
    purchase_order: MatchedPO | None = Field(
        default=None, description="Set only when exactly one PO has the reference"
    )
    candidates: list[MatchedPO] = Field(
        default_factory=list, description="Every PO with the reference; more than one means AMBIGUOUS"
    )
    reasons: list[MatchReason] = Field(default_factory=list)

    @computed_field
    @property
    def is_matched(self) -> bool:
        return self.status is POMatchStatus.MATCHED

    @property
    def errors(self) -> list[MatchReason]:
        return [r for r in self.reasons if r.severity is Severity.ERROR]
