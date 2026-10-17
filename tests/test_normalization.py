from datetime import date, datetime
from decimal import Decimal

import pytest

from app.normalization import (
    normalize_amount,
    normalize_currency,
    normalize_date,
    normalize_invoice,
    normalize_quantity,
    normalize_reference,
    normalize_vendor_name,
    normalize_whitespace,
    po_reference_key,
    reference_key,
    vendor_identity,
)
from app.schemas import ExtractedInvoice, ExtractedLineItem

# --- whitespace ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("  Acme  Office\tSupplies\n", "Acme Office Supplies"),
        ("Acme​Office", "AcmeOffice"),
        ("Acme & Co. (India)", "Acme & Co. (India)"),
        ("Café", "Café"),  # combining accent composed (NFC), not dropped
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_whitespace(value, expected):
    assert normalize_whitespace(value) == expected


# --- references ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("inv-1001", "INV-1001"),
        ("  INV – 1001. ", "INV-1001"),
        ("INV‑1001", "INV-1001"),  # non-breaking hyphen
        ("BW / 2026 / 0457", "BW/2026/0457"),
        ("INV_2026.01", "INV_2026.01"),
        ("(A-77)", "A-77"),
        ("INV 1001", "INV 1001"),  # inner space kept: not provably a separator
        ("...", None),
        (None, None),
    ],
)
def test_normalize_reference(value, expected):
    assert normalize_reference(value) == expected


@pytest.mark.parametrize("value", ["INV-1001", "inv/1001", "INV 1001", "INV1001", "INV_1001", "INV – 1001"])
def test_reference_key_equivalents(value):
    assert reference_key(value) == "INV-1001"


@pytest.mark.parametrize(
    "a, b",
    [("INV-1001", "INV-10-01"), ("BW/2026/0457", "BW/2026/457"), ("INV-1001", "INV-1002"), ("A1B2", "AB12")],
)
def test_reference_key_keeps_meaningful_differences(a, b):
    assert reference_key(a) != reference_key(b)


@pytest.mark.parametrize(
    "value", ["PO-5001", "PO 5001", "po-5001", "PO5001", "PO#5001", "P.O. 5001", "P.O. No. 5001", "PO No: 5001",
              "Purchase Order 5001", "PO – 5001", "5001"]
)
def test_po_reference_key_equivalents(value):
    assert po_reference_key(value) == "5001"


@pytest.mark.parametrize(
    "value, expected",
    [("PO-2026-01", "2026-01"), ("PO/2026/01", "2026-01"), ("PO-05001", "05001"), ("POS-1", "POS-1"), ("PO", None), ("", None), (None, None)],
)
def test_po_reference_key(value, expected):
    assert po_reference_key(value) == expected


# --- vendors ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Acme Office Supplies Pvt Ltd",
        "ACME OFFICE SUPPLIES PRIVATE LIMITED",
        "Acme Office Supplies Pvt. Ltd.",
        "Acme  Office Supplies (P) Ltd",
        "M/s Acme Office Supplies Pvt Ltd",
        "acme office supplies pvt limited",
    ],
)
def test_vendor_identity_equivalents(value):
    identity = vendor_identity(value)
    assert identity.core == "acme office supplies"
    assert identity.legal_suffix == "pvt ltd"
    assert identity.key == "acme office supplies pvt ltd"


def test_vendor_identity_distinguishes_legal_forms():
    assert vendor_identity("Acme Office Supplies LLP").key != vendor_identity("Acme Office Supplies Pvt Ltd").key
    assert vendor_identity("Acme Ltd").key != vendor_identity("Acme Pvt Ltd").key


def test_vendor_identity_without_suffix():
    identity = vendor_identity("Acme Office Supplies")
    assert (identity.core, identity.legal_suffix) == ("acme office supplies", None)


def test_vendor_identity_folds_accents_and_ampersand():
    assert vendor_identity("Café & Bakery Limited").key == vendor_identity("Cafe and Bakery Ltd").key


def test_vendor_identity_name_that_is_only_a_suffix():
    assert vendor_identity("Limited").key == "limited"


@pytest.mark.parametrize("value", [None, "", "  ", "&", "..."])
def test_vendor_identity_empty(value):
    assert vendor_identity(value) is None


def test_vendor_display_name_keeps_characters():
    assert normalize_vendor_name("  M/s Acme & Co. (India) ") == "M/s Acme & Co. (India)"


# --- currency --------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [("INR", "INR"), (" inr ", "INR"), ("₹", "INR"), ("Rs.", "INR"), ("Rupees", "INR"), ("US$", "USD"), ("€", "EUR"), (None, None)],
)
def test_normalize_currency(value, expected):
    assert normalize_currency(value) == expected


@pytest.mark.parametrize("value, message", [("$", "does not identify"), ("Dollars", "unknown currency"), ("XYZ", "unknown")])
def test_normalize_currency_rejects_unclear(value, message):
    with pytest.raises(ValueError, match=message):
        normalize_currency(value)


# --- money -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (Decimal("50000"), "50000.00"),
        (Decimal("42372.88"), "42372.88"),
        (Decimal("7.5"), "7.50"),
        (120000, "120000.00"),
        ("1,20,000", "120000.00"),
        ("Rs. 1,234.5", "1234.50"),
        (Decimal("-15.5"), "-15.50"),
    ],
)
def test_normalize_amount(value, expected):
    result = normalize_amount(value)
    assert isinstance(result, Decimal)
    assert str(result) == expected


@pytest.mark.parametrize("value, error", [(Decimal("1.005"), ValueError), ("12.345", ValueError), (Decimal("NaN"), ValueError),
                                          ("abc", ValueError), (1.5, TypeError), (True, TypeError)])
def test_normalize_amount_rejects_without_rounding(value, error):
    with pytest.raises(error):
        normalize_amount(value)


def test_normalize_quantity_keeps_precision():
    assert str(normalize_quantity(Decimal("2.5"))) == "2.5"
    assert str(normalize_quantity("447.7425")) == "447.7425"
    with pytest.raises(ValueError):
        normalize_quantity(Decimal("1.00001"))


# --- dates -----------------------------------------------------------------------


def test_normalize_date():
    assert normalize_date(date(2026, 9, 15)) == date(2026, 9, 15)
    assert normalize_date(datetime(2026, 9, 15, 10, 30)) == date(2026, 9, 15)
    assert normalize_date(" 15/09/2026 ") == date(2026, 9, 15)
    assert normalize_date("09/15/2026", "MDY") == date(2026, 9, 15)
    assert normalize_date(None) is None
    with pytest.raises(ValueError):
        normalize_date("31/02/2026")


# --- whole invoice -----------------------------------------------------------------


def test_normalize_invoice():
    extracted = ExtractedInvoice(
        vendor_name="  Acme Office Supplies Pvt Ltd ",
        invoice_number="inv – 1001",
        invoice_date=date(2026, 9, 15),
        po_number="po 5001",
        currency="inr",
        subtotal=Decimal("42372.88"),
        tax_amount=Decimal("7627.12"),
        total_amount=Decimal("50000"),
        line_items=[ExtractedLineItem(description=" A4  Paper ", quantity=Decimal("100"), unit_price=Decimal("250"), amount=Decimal("25000"))],
    )

    result = normalize_invoice(extracted)
    inv = result.invoice

    assert result.warnings == []
    assert inv.vendor_name == "Acme Office Supplies Pvt Ltd"
    assert inv.invoice_number == "INV-1001"
    assert inv.po_number == "PO 5001"
    assert po_reference_key(inv.po_number) == "5001"
    assert inv.currency == "INR"
    assert str(inv.total_amount) == "50000.00"
    assert inv.line_items[0].description == "A4 Paper"
    assert str(inv.line_items[0].amount) == "25000.00"
    assert inv.line_items[0].unit_price == Decimal("250")


def test_normalize_invoice_unclear_values_become_none_with_warning():
    result = normalize_invoice(ExtractedInvoice(currency="$", total_amount=Decimal("10.005")))

    assert result.invoice.currency is None
    assert result.invoice.total_amount is None
    assert any(w.startswith("currency:") for w in result.warnings)
    assert any(w.startswith("total_amount:") for w in result.warnings)


def test_normalize_invoice_all_missing():
    result = normalize_invoice(ExtractedInvoice())
    assert result.warnings == []
    assert result.invoice.model_dump(exclude={"line_items"}) == dict.fromkeys(result.invoice.model_dump(exclude={"line_items"}))
