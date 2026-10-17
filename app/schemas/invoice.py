"""Pydantic schemas shared across pipeline stages and the API."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import POStatus


class LineItem(BaseModel):
    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


class NormalizedInvoice(BaseModel):
    """Typed, cleaned invoice fields ready for matching and validation."""

    invoice_number: str | None = None
    vendor_name: str | None = None
    invoice_date: date | None = None
    po_number: str | None = None
    currency: str | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    line_items: list[LineItem] = Field(default_factory=list)


class NormalizationResult(BaseModel):
    invoice: NormalizedInvoice
    warnings: list[str] = Field(default_factory=list)


class PurchaseOrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    po_number: str
    vendor_name: str
    currency: str
    total_amount: Decimal
    status: POStatus
