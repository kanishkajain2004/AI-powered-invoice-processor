"""Normalization: ExtractedInvoice -> NormalizedInvoice.

Applies the value normalizers field by field. A field that cannot be normalized is
set to None with a warning, rather than being passed on in an uncertain form.
"""

from collections.abc import Callable
from typing import Any

from app.config import get_settings
from app.normalization.values import (
    normalize_amount,
    normalize_currency,
    normalize_date,
    normalize_quantity,
    normalize_reference,
    normalize_vendor_name,
    normalize_whitespace,
)
from app.schemas import ExtractedInvoice, LineItem, NormalizationResult, NormalizedInvoice


def _safe(name: str, func: Callable[[Any], Any], value: Any, warnings: list[str]) -> Any:
    try:
        return func(value)
    except (ValueError, TypeError) as exc:
        warnings.append(f"{name}: {exc}; left empty.")
        return None


def normalize_invoice(extracted: ExtractedInvoice) -> NormalizationResult:
    warnings: list[str] = []
    date_order = get_settings().date_order

    line_items = []
    for index, item in enumerate(extracted.line_items, start=1):
        line_items.append(
            LineItem(
                description=normalize_whitespace(item.description) or "",
                quantity=_safe(f"line {index} quantity", normalize_quantity, item.quantity, warnings),
                unit_price=_safe(f"line {index} unit_price", normalize_quantity, item.unit_price, warnings),
                amount=_safe(f"line {index} amount", normalize_amount, item.amount, warnings),
            )
        )

    invoice = NormalizedInvoice(
        invoice_number=normalize_reference(extracted.invoice_number),
        vendor_name=normalize_vendor_name(extracted.vendor_name),
        invoice_date=_safe("invoice_date", lambda v: normalize_date(v, date_order), extracted.invoice_date, warnings),
        po_number=normalize_reference(extracted.po_number),
        currency=_safe("currency", normalize_currency, extracted.currency, warnings),
        subtotal=_safe("subtotal", normalize_amount, extracted.subtotal, warnings),
        tax_amount=_safe("tax_amount", normalize_amount, extracted.tax_amount, warnings),
        total_amount=_safe("total_amount", normalize_amount, extracted.total_amount, warnings),
        line_items=line_items,
    )
    return NormalizationResult(invoice=invoice, warnings=warnings)
