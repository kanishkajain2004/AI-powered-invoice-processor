"""Schemas produced by the extraction stage.

Every field is optional: a value that is missing from the document, or that could
not be read unambiguously, is None. The extractor never fills a field by guessing;
when it leaves a field empty for a reason worth knowing, it says why in `warnings`.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field


class ExtractedLineItem(BaseModel):
    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


class ExtractedInvoice(BaseModel):
    vendor_name: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    po_number: str | None = None
    currency: str | None = Field(default=None, description="ISO 4217 code, e.g. INR")
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    line_items: list[ExtractedLineItem] = Field(default_factory=list)


class ExtractionMethod(StrEnum):
    TEXT = "text"  # every page had a usable text layer
    OCR = "ocr"  # every non-blank page was OCR'd
    MIXED = "mixed"  # some pages each way


class ExtractionResult(BaseModel):
    invoice: ExtractedInvoice
    method: ExtractionMethod
    page_count: int
    ocr_pages: list[int] = Field(default_factory=list, description="1-based page numbers")
    raw_text: str
    raw_fields: dict[str, str] = Field(
        default_factory=dict, description="Source text each populated header field was parsed from"
    )
    warnings: list[str] = Field(default_factory=list)
