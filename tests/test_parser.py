"""Unit tests for the deterministic parser, using synthetic text lines.

In the test text, two or more spaces separate segments (cells/columns); each
character is treated as 5pt wide so vertical alignment between lines is preserved.
"""

import re
import textwrap
from datetime import date
from decimal import Decimal

import pytest

from app.extraction.layout import Line, Segment
from app.extraction.parser import parse_date, parse_identifier, parse_invoice, parse_number

CHAR = 5.0


def lines_from_text(text: str) -> list[Line]:
    lines = []
    for row in textwrap.dedent(text).strip("\n").splitlines():
        segments = [Segment(m[0], m.start() * CHAR, m.end() * CHAR) for m in re.finditer(r"\S+(?: \S+)*", row)]
        if segments:
            lines.append(Line(segments))
    return lines


def parse(text: str, **kwargs):
    return parse_invoice(lines_from_text(text), **kwargs)


# --- primitives ------------------------------------------------------------------


@pytest.mark.parametrize(
    "token, expected",
    [
        ("1,234.56", "1234.56"),
        ("1,234,567.89", "1234567.89"),
        ("12,34,567.89", "1234567.89"),
        ("1,20,000", "120000"),
        ("500", "500"),
        ("Rs.500", "500"),
        ("₹1,000.00", "1000.00"),
        ("INR50", "50"),
        ("(250.00)", "-250.00"),
        ("-12.5", "-12.5"),
        ("5,000/-", "5000"),
        ("42.00:", "42.00"),
    ],
)
def test_parse_number(token, expected):
    assert parse_number(token) == Decimal(expected)


@pytest.mark.parametrize("token", ["18%", "12,34", "1,2345", "15/09/2026", "abc", "1.234", "", "-", "INR"])
def test_parse_number_rejects_non_amounts(token):
    assert parse_number(token) is None


def test_parse_number_allows_more_places_when_asked():
    assert parse_number("0.1250", max_places=4) == Decimal("0.1250")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("15/09/2026", date(2026, 9, 15)),
        ("15-09-2026", date(2026, 9, 15)),
        ("15.09.2026", date(2026, 9, 15)),
        ("2026-09-15", date(2026, 9, 15)),
        ("02 Oct 2026", date(2026, 10, 2)),
        ("2nd October, 2026", date(2026, 10, 2)),
        ("15-Sep-2026", date(2026, 9, 15)),
        ("October 2, 2026", date(2026, 10, 2)),
        ("Sept 2 2026", date(2026, 9, 2)),
        ("on 01/12/2026 (Tuesday)", date(2026, 12, 1)),
    ],
)
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", ["31/02/2026", "09/15/2026", "15/09/26", "next Tuesday", "", "32 Foo 2026"])
def test_parse_date_returns_none_when_invalid_or_unclear(text):
    assert parse_date(text) is None


def test_parse_date_respects_mdy_order():
    assert parse_date("09/15/2026", "MDY") == date(2026, 9, 15)
    assert parse_date("03/04/2026", "MDY") == date(2026, 3, 4)
    assert parse_date("03/04/2026", "DMY") == date(2026, 4, 3)


@pytest.mark.parametrize(
    "text, expected",
    [("INV-1001 extra words", "INV-1001"), ("BW/2026/0457", "BW/2026/0457"), ("A77.", "A77"), ("Pending", None), ("", None)],
)
def test_parse_identifier(text, expected):
    assert parse_identifier(text) == expected


# --- header fields -----------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("Invoice No: INV-1", "INV-1"),
        ("Invoice No.: INV-1", "INV-1"),
        ("Invoice Number INV-1", "INV-1"),
        ("Invoice #: INV-1", "INV-1"),
        ("Invoice ID: INV-1", "INV-1"),
        ("Invoice: INV-1", "INV-1"),
        ("Bill No. INV-1", "INV-1"),
        ("Tax Invoice No: INV-1", "INV-1"),
        ("Invoice No:   INV-1", "INV-1"),  # value in the next column
    ],
)
def test_invoice_number_labels(line, expected):
    assert parse(line).invoice.invoice_number == expected


def test_value_below_label():
    result = parse(
        """
        Invoice No.     Invoice Date
        INV-42          05/01/2026
        """
    )
    assert result.invoice.invoice_number == "INV-42"
    assert result.invoice.invoice_date == date(2026, 1, 5)


def test_label_followed_by_another_label_is_not_a_value():
    result = parse("Invoice No:   Invoice Date: 05/01/2026")
    assert result.invoice.invoice_number is None
    assert result.invoice.invoice_date == date(2026, 1, 5)
    assert any("invoice_number" in w for w in result.warnings)


def test_title_is_not_an_invoice_number():
    result = parse("TAX INVOICE\nINVOICE")
    assert result.invoice.invoice_number is None
    assert result.warnings == []


def test_due_date_and_po_date_are_not_invoice_date():
    result = parse(
        """
        Due Date: 30/09/2026
        PO Date: 01/09/2026
        """
    )
    assert result.invoice.invoice_date is None
    assert result.invoice.po_number is None


def test_invoice_date_preferred_over_generic_date():
    result = parse(
        """
        Invoice Date: 15/09/2026
        Date: 16/09/2026
        """
    )
    assert result.invoice.invoice_date == date(2026, 9, 15)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("PO Number: PO-5001", "PO-5001"),
        ("PO No: 5001", "5001"),
        ("P.O. No.: PO-5001", "PO-5001"),
        ("PO#: PO-5001", "PO-5001"),
        ("Purchase Order: PO-5001", "PO-5001"),
        ("Purchase Order No.   PO-5001", "PO-5001"),
        ("PO Ref: PO-5001", "PO-5001"),
        ("Against your order PO-5001 dated 1 Sep", "PO-5001"),  # bare token, no label
    ],
)
def test_po_number(line, expected):
    assert parse(line).invoice.po_number == expected


def test_po_box_is_not_a_po_number():
    assert parse("P.O. Box 1234, Mumbai").invoice.po_number is None


def test_several_bare_po_tokens_are_ambiguous():
    result = parse("Covers PO-5001 and PO-5004")
    assert result.invoice.po_number is None
    assert any("several PO references" in w for w in result.warnings)


def test_conflicting_values_give_none_with_warning():
    result = parse(
        """
        Invoice No: INV-1
        Invoice No: INV-2
        """
    )
    assert result.invoice.invoice_number is None
    assert any("conflicting" in w for w in result.warnings)


def test_repeated_identical_value_is_fine():
    result = parse(
        """
        Invoice No: INV-1
        Invoice No: INV-1
        """
    )
    assert result.invoice.invoice_number == "INV-1"
    assert result.warnings == []


@pytest.mark.parametrize("line", ["Invoice Date: 31/02/2026", "Invoice Date: soon", "Invoice Date: 09/15/2026"])
def test_unreadable_date_is_none_with_warning(line):
    result = parse(line)
    assert result.invoice.invoice_date is None
    assert any("invoice_date" in w for w in result.warnings)


def test_date_order_setting():
    assert parse("Invoice Date: 09/15/2026", date_order="MDY").invoice.invoice_date == date(2026, 9, 15)


# --- vendor ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Vendor: Acme Corp",
        "Vendor Name: Acme Corp",
        "Supplier   Acme Corp",
        "Sold By: Acme Corp",
        "From: Acme Corp",
        "Supplier:\nAcme Corp",
    ],
)
def test_vendor_labels(text):
    assert parse(text).invoice.vendor_name == "Acme Corp"


def test_vendor_from_letterhead_not_buyer():
    result = parse(
        """
        Acme Office Supplies Pvt Ltd
        12 MG Road
        Bill To:
        Zamp Demo Buyer Pvt Ltd
        """
    )
    assert result.invoice.vendor_name == "Acme Office Supplies Pvt Ltd"


def test_vendor_code_is_not_vendor_name():
    result = parse(
        """
        Bluewave Technologies LLP
        Vendor Code: V-12
        """
    )
    assert result.invoice.vendor_name == "Bluewave Technologies LLP"


def test_vendor_none_when_only_buyer_has_company_name():
    result = parse(
        """
        INVOICE
        Bill To:
        Zamp Demo Buyer Pvt Ltd
        """
    )
    assert result.invoice.vendor_name is None


def test_vendor_none_without_label_or_company_suffix():
    assert parse("Some Shop\nInvoice No: 1").invoice.vendor_name is None


# --- currency -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Currency: INR", "INR"),
        ("Currency: usd", "USD"),
        ("Total  ₹ 500.00", "INR"),
        ("Total  Rs. 500.00", "INR"),
        ("Amount in words: Rupees Five Hundred", "INR"),
        ("Total  US$ 500.00", "USD"),
        ("Total  EUR 500.00", "EUR"),
        ("Total  £500.00", "GBP"),
    ],
)
def test_currency(text, expected):
    assert parse(text).invoice.currency == expected


def test_bare_dollar_is_not_guessed():
    result = parse("Total  $500.00")
    assert result.invoice.currency is None
    assert any("'$'" in w for w in result.warnings)


def test_mixed_currencies_give_none():
    result = parse("Total  INR 500.00\nPaid  USD 6.00")
    assert result.invoice.currency is None
    assert any("several currencies" in w for w in result.warnings)


def test_unknown_currency_label_value_gives_none():
    result = parse("Currency: Dollars")
    assert result.invoice.currency is None
    assert any("currency" in w for w in result.warnings)


def test_no_currency_anywhere():
    result = parse("Total  500.00")
    assert result.invoice.currency is None
    assert result.warnings == []


# --- totals ---------------------------------------------------------------------


def test_subtotal_tax_total_are_distinguished():
    result = parse(
        """
        Subtotal          1,000.00
        Total Tax           180.00
        Total             1,180.00
        """
    )
    inv = result.invoice
    assert (inv.subtotal, inv.tax_amount, inv.total_amount) == (Decimal("1000.00"), Decimal("180.00"), Decimal("1180.00"))


def test_cgst_and_sgst_are_summed():
    result = parse(
        """
        Taxable Value   1,00,000.00
        CGST @ 9%          9,000.00
        SGST @ 9%          9,000.00
        """
    )
    assert result.invoice.subtotal == Decimal("100000.00")
    assert result.invoice.tax_amount == Decimal("18000.00")


def test_igst_alone():
    assert parse("IGST 18%   1,800.00").invoice.tax_amount == Decimal("1800.00")


def test_total_tax_line_wins_over_components():
    result = parse(
        """
        CGST   90.00
        SGST   90.00
        Total Tax   180.00
        """
    )
    assert result.invoice.tax_amount == Decimal("180.00")


def test_gst_plus_breakdown_is_ambiguous():
    result = parse(
        """
        GST 18%   180.00
        CGST 9%    90.00
        SGST 9%    90.00
        """
    )
    assert result.invoice.tax_amount is None
    assert any("breakdown" in w for w in result.warnings)


def test_gstin_and_tax_rate_are_not_tax_amounts():
    result = parse(
        """
        GSTIN: 29AACCA1234F1Z5
        Tax Rate: 18%
        Tax Invoice No: 77
        """
    )
    assert result.invoice.tax_amount is None


def test_grand_total_preferred_over_total():
    result = parse(
        """
        Total         1,000.00
        Grand Total   1,180.00
        """
    )
    assert result.invoice.total_amount == Decimal("1180.00")


def test_conflicting_totals_give_none():
    result = parse(
        """
        Total   1,000.00
        Total   1,180.00
        """
    )
    assert result.invoice.total_amount is None
    assert any("total_amount: conflicting" in w for w in result.warnings)


def test_total_label_without_amount_is_ignored():
    result = parse(
        """
        Total (in words): One thousand only
        Amount Due   1,000.00
        """
    )
    assert result.invoice.total_amount == Decimal("1000.00")


def test_totals_with_text_on_the_left():
    result = parse("Thank you for your business      Subtotal   500.00")
    assert result.invoice.subtotal == Decimal("500.00")


# --- line items -------------------------------------------------------------------

TABLE = """
#   Description             Qty   Unit Price      Amount
1   A4 Paper (500 sheets)   100       250.00   25,000.00
2   Pens, Box of 50          40   INR 300.00   12,000.00
Subtotal                                       37,000.00
"""


def test_line_items():
    items = parse(TABLE).invoice.line_items
    assert [(i.description, i.quantity, i.unit_price, i.amount) for i in items] == [
        ("A4 Paper (500 sheets)", Decimal("100"), Decimal("250.00"), Decimal("25000.00")),
        ("Pens, Box of 50", Decimal("40"), Decimal("300.00"), Decimal("12000.00")),
    ]


def test_table_rows_are_not_read_as_header_fields():
    result = parse(TABLE)
    assert result.invoice.subtotal == Decimal("37000.00")
    assert result.invoice.total_amount is None


def test_leading_number_kept_without_serial_column():
    items = parse(
        """
        Description        Qty   Rate    Amount
        2 Port USB Hub       3   500.00  1,500.00
        """
    ).invoice.line_items
    assert items[0].description == "2 Port USB Hub"


def test_wrapped_description_is_joined():
    items = parse(
        """
        Description          Qty   Rate     Amount
        Heavy Duty Stapler    12   447.74   5,372.88
        with 5000 staples
        Total                               5,372.88
        """
    ).invoice.line_items
    assert items[0].description == "Heavy Duty Stapler with 5000 staples"


def test_unreadable_row_is_skipped_with_warning():
    result = parse(
        """
        Description   Qty       Rate     Amount
        Paper         100 pcs   250.00   25,000.00
        Pens          40        300.00   12,000.00
        Total                            37,000.00
        """
    )
    assert [i.description for i in result.invoice.line_items] == ["Pens"]
    assert any("could not read table row" in w for w in result.warnings)


def test_repeated_header_on_next_page_is_skipped():
    items = parse(
        """
        Description   Qty   Rate     Amount
        Paper         100   250.00   25,000.00
        Description   Qty   Rate     Amount
        Pens           40   300.00   12,000.00
        Total                        37,000.00
        """
    ).invoice.line_items
    assert [i.description for i in items] == ["Paper", "Pens"]


def test_no_table_means_no_line_items():
    assert parse("Invoice No: 1\nTotal  100.00").invoice.line_items == []


def test_empty_input():
    result = parse_invoice([])
    assert result.invoice.model_dump(exclude={"line_items"}) == dict.fromkeys(
        ["vendor_name", "invoice_number", "invoice_date", "po_number", "currency", "subtotal", "tax_amount", "total_amount"]
    )
    assert result.invoice.line_items == []
    assert result.warnings == []


def test_vendor_under_a_from_column_heading():
    result = parse(
        """
        FROM                            BILL TO                     INVOICE DETAILS
        Acme Office Supplies Pvt Ltd    Demo Manufacturing Pvt Ltd  Invoice No: INV-1001
        """
    )
    assert result.invoice.vendor_name == "Acme Office Supplies Pvt Ltd"
    assert result.invoice.invoice_number == "INV-1001"


def test_from_inside_a_sentence_is_not_a_vendor_label():
    assert parse("From 1 Sep to 30 Sep\nInvoice No: 1").invoice.vendor_name is None


def test_sentence_starting_with_currency_is_not_a_currency_label():
    result = parse(
        """
        Currency: USD
        Currency intentionally differs from the purchase order.
        """
    )
    assert result.invoice.currency == "USD"
    assert result.warnings == []
