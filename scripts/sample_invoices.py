"""Realistic demo invoices for the case study, built as real PDFs.

Each invoice is declared as an InvoiceSpec (parties, header fields, line items, tax
lines). Line amounts, subtotal, tax and total are computed from the line items with
Decimal arithmetic, so every document is internally consistent unless a scenario
deliberately says otherwise. Two layouts are used, both supported by the extractor:

* layout "letterhead": vendor letterhead, "Label: value" header pairs in two columns,
  Bill To / Ship To blocks, a serial-numbered item table (Acme Office Supplies).
* layout "columns": a "Supplier:" block with the value below the label, and header
  labels and values in separate columns (Bluewave Technologies).

All companies, addresses, GSTINs and bank details are fictional.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import pymupdf

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4, points
FONT, BOLD = "helv", "hebo"
CENT = Decimal("0.01")
INK, MUTED, RULE = (0.1, 0.1, 0.1), (0.38, 0.38, 0.38), (0.75, 0.75, 0.75)


@dataclass(frozen=True)
class Party:
    name: str
    address: tuple[str, ...]
    gstin: str
    pan: str | None = None
    contact: str | None = None


@dataclass(frozen=True)
class Line:
    description: str
    quantity: int
    unit_price: Decimal
    note: str | None = None  # printed on a second line under the description

    @property
    def amount(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(CENT, ROUND_HALF_UP)


@dataclass(frozen=True)
class Tax:
    label: str  # e.g. "CGST @ 9%"
    rate: Decimal  # percent


@dataclass
class InvoiceSpec:
    layout: str  # "letterhead" | "columns"
    vendor: Party
    bill_to: Party
    ship_to: Party
    invoice_number: str | None
    invoice_date: str | None
    due_date: str | None
    po_number: str | None
    place_of_supply: str
    lines: list[Line]
    taxes: list[Tax]
    currency: str = "INR"
    banner: str | None = None  # e.g. "COPY - RESENT", printed under the title
    misprinted_tax: Decimal | None = None  # print this tax figure instead of the correct one; total stays correct
    bank: str = ""
    extra_notes: list[str] = field(default_factory=list)

    @property
    def subtotal(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0.00"))

    @property
    def tax_amounts(self) -> list[tuple[Tax, Decimal]]:
        return [(t, (self.subtotal * t.rate / 100).quantize(CENT, ROUND_HALF_UP)) for t in self.taxes]

    @property
    def total(self) -> Decimal:
        return self.subtotal + sum((amount for _, amount in self.tax_amounts), Decimal("0.00"))


# --- formatting ---------------------------------------------------------------------------------


def _indian(amount: Decimal) -> str:
    """1,18,000.00 - how Indian vendors print rupee amounts."""
    whole, frac = f"{amount:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{whole}.{frac}"


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _words_below_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def money_text(amount: Decimal, currency: str) -> str:
    """Indian digit grouping for rupees, Western grouping for other currencies."""
    return _indian(amount) if currency == "INR" else f"{amount:,.2f}"


def amount_in_words(amount: Decimal, currency: str) -> str:
    if currency == "INR":
        return rupees_in_words(amount)
    whole, cents = int(amount), int((amount - int(amount)) * 100)
    parts = []
    for size, name in ((1_000_000, "Million"), (1_000, "Thousand")):
        if whole >= size:
            parts.append(f"{_words_below_1000(whole // size)} {name}")
            whole %= size
    if whole:
        parts.append(_words_below_1000(whole))
    text = {"USD": "US Dollars"}.get(currency, currency) + " " + " ".join(parts)
    if cents:
        text += f" and {_words_below_1000(cents)} Cents"
    return text + " Only"


def rupees_in_words(amount: Decimal) -> str:
    rupees, paise = int(amount), int((amount - int(amount)) * 100)
    parts = []
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        if rupees >= size:
            parts.append(f"{_words_below_1000(rupees // size)} {name}")
            rupees %= size
    if rupees:
        parts.append(_words_below_1000(rupees))
    text = "Rupees " + " ".join(parts)
    if paise:
        text += f" and {_words_below_1000(paise)} Paise"
    return text + " Only"


# --- drawing --------------------------------------------------------------------------------------


class _Canvas:
    def __init__(self, page: pymupdf.Page):
        self.page = page

    def text(self, x, y, text, size=9.5, bold=False, right=False, color=INK):
        font = BOLD if bold else FONT
        if right:
            x -= pymupdf.get_text_length(text, fontname=font, fontsize=size)
        self.page.insert_text((x, y), text, fontname=font, fontsize=size, color=color)

    def rule(self, y, x0=40, x1=PAGE_WIDTH - 40, width=0.6):
        self.page.draw_line((x0, y), (x1, y), color=RULE, width=width)

    def box(self, x0, y0, x1, y1, fill=(0.95, 0.95, 0.94)):
        self.page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=None, fill=fill)


def _party_block(c: _Canvas, x: float, y: float, title: str, party: Party) -> float:
    c.text(x, y, title, bold=True)
    y += 14
    c.text(x, y, party.name, bold=True)
    for line in party.address:
        y += 12
        c.text(x, y, line, color=MUTED)
    y += 12
    c.text(x, y, f"GSTIN: {party.gstin}", color=MUTED)
    return y


def _items_and_totals(c: _Canvas, spec: InvoiceSpec, y: float) -> float:
    cur = spec.currency
    c.box(40, y - 11, PAGE_WIDTH - 40, y + 5)
    c.text(48, y, "#", bold=True)
    c.text(72, y, "Description", bold=True)
    c.text(370, y, "Qty", bold=True, right=True)
    c.text(460, y, f"Unit Price ({cur})", bold=True, right=True)
    c.text(PAGE_WIDTH - 48, y, f"Amount ({cur})", bold=True, right=True)
    y += 20
    for number, line in enumerate(spec.lines, start=1):
        c.text(48, y, str(number))
        c.text(72, y, line.description)
        c.text(370, y, str(line.quantity), right=True)
        c.text(460, y, money_text(line.unit_price, cur), right=True)
        c.text(PAGE_WIDTH - 48, y, money_text(line.amount, cur), right=True)
        if line.note:
            y += 12
            c.text(72, y, line.note, size=8.5, color=MUTED)
        y += 18
    c.rule(y - 8)

    y += 8
    taxes = [(t.label, amount if spec.misprinted_tax is None else spec.misprinted_tax)
             for t, amount in spec.tax_amounts]
    rows = [("Subtotal", spec.subtotal)] + taxes
    for label, amount in rows:
        c.text(380, y, label)
        c.text(PAGE_WIDTH - 48, y, money_text(amount, cur), right=True)
        y += 15
    c.box(372, y - 11, PAGE_WIDTH - 40, y + 6, fill=(0.9, 0.92, 0.96))
    c.text(380, y, f"Total ({cur})", bold=True)
    c.text(PAGE_WIDTH - 48, y, money_text(spec.total, cur), bold=True, right=True)
    y += 26
    c.text(48, y, f"Amount in words: {amount_in_words(spec.total, cur)}", size=9)
    return y


def _footer(c: _Canvas, spec: InvoiceSpec, y: float) -> None:
    y += 28
    c.text(48, y, "Payment terms: Net 30 days. Please quote our reference with your remittance.", size=8.5, color=MUTED)
    for note in spec.extra_notes:
        y += 12
        c.text(48, y, note, size=8.5, color=MUTED)
    y += 12
    c.text(48, y, spec.bank, size=8.5, color=MUTED)
    y += 46
    c.text(PAGE_WIDTH - 48, y, f"For {spec.vendor.name}", right=True, size=9)
    y += 30
    c.text(PAGE_WIDTH - 48, y, "Authorised Signatory", right=True, size=9, color=MUTED)
    c.text(48, PAGE_HEIGHT - 30, "This is a computer-generated invoice.", size=7.5, color=MUTED)


def _letterhead(c: _Canvas, spec: InvoiceSpec) -> None:
    v = spec.vendor
    c.text(48, 62, v.name, size=17, bold=True)
    c.text(PAGE_WIDTH - 48, 62, "TAX INVOICE", size=15, bold=True, right=True)
    if spec.banner:
        c.text(PAGE_WIDTH - 48, 78, spec.banner, size=9, bold=True, right=True, color=(0.7, 0.15, 0.15))
    y = 80
    for line in v.address:
        c.text(48, y, line, color=MUTED)
        y += 12
    c.text(48, y, f"GSTIN: {v.gstin}    PAN: {v.pan}", color=MUTED)
    if v.contact:
        y += 12
        c.text(48, y, v.contact, color=MUTED)
    c.rule(y + 12, width=1.0)

    y += 34
    pairs = [("Invoice No", spec.invoice_number, "Invoice Date", spec.invoice_date),
             ("PO Number", spec.po_number, "Due Date", spec.due_date),
             ("Currency", spec.currency, "Place of Supply", spec.place_of_supply)]
    for left_label, left, right_label, right in pairs:
        c.text(48, y, f"{left_label}: {left or ''}".rstrip())
        c.text(330, y, f"{right_label}: {right or ''}".rstrip())
        y += 15

    y += 14
    bottom = max(_party_block(c, 48, y, "Bill To:", spec.bill_to), _party_block(c, 330, y, "Ship To:", spec.ship_to))
    y = _items_and_totals(c, spec, bottom + 34)
    _footer(c, spec, y)


def _columns(c: _Canvas, spec: InvoiceSpec) -> None:
    title_width = pymupdf.get_text_length("TAX INVOICE", fontname=BOLD, fontsize=15)
    c.text((PAGE_WIDTH - title_width) / 2, 56, "TAX INVOICE", size=15, bold=True)
    c.rule(78, width=1.0)

    v = spec.vendor
    c.text(48, 100, "Supplier:", bold=True)
    c.text(48, 114, v.name, size=12, bold=True)
    y = 128
    for line in v.address:
        c.text(48, y, line, color=MUTED)
        y += 12
    c.text(48, y, f"GSTIN: {v.gstin}", color=MUTED)
    if v.contact:
        y += 12
        c.text(48, y, v.contact, color=MUTED)

    rows = [("Bill No.", spec.invoice_number), ("Dated", spec.invoice_date), ("P.O. No.", spec.po_number),
            ("Due Date", spec.due_date), ("Currency", spec.currency)]
    ry = 100
    for label, value in rows:
        c.text(340, ry, label, bold=True)
        if value:
            c.text(430, ry, value)
        ry += 16
    c.text(340, ry, "Place of Supply", bold=True)
    c.text(430, ry, spec.place_of_supply)

    y = max(y, ry) + 34
    bottom = max(_party_block(c, 48, y, "Buyer:", spec.bill_to), _party_block(c, 340, y, "Consignee:", spec.ship_to))
    y = _items_and_totals(c, spec, bottom + 34)
    _footer(c, spec, y)


def build_pdf(spec: InvoiceSpec) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    {"letterhead": _letterhead, "columns": _columns}[spec.layout](_Canvas(page), spec)
    doc.set_metadata({"title": f"Invoice {spec.invoice_number or '(unnumbered)'}", "author": spec.vendor.name,
                      "creator": "Zamp case study sample generator", "producer": "PyMuPDF"})
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


def scan(pdf: bytes, dpi: int = 200) -> bytes:
    """Rasterize each page into a greyscale image with no text layer, as a document scanner would."""
    src = pymupdf.open(stream=pdf, filetype="pdf")
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=pix.tobytes("jpeg", jpg_quality=85))
    out.set_metadata({"title": "Scanned document", "creator": "Zamp case study sample generator (scan)"})
    data = out.tobytes(garbage=3, deflate=True)
    src.close()
    out.close()
    return data


# --- the demo cast ----------------------------------------------------------------------------------

ACME = Party(
    "Acme Office Supplies Pvt Ltd",
    ("12 MG Road, Ashok Nagar", "Bengaluru, Karnataka 560001"),
    gstin="29AACCA1234F1Z5", pan="AACCA1234F", contact="accounts@acme-supplies.example · +91 80 4000 1234",
)
BLUEWAVE = Party(
    "Bluewave Technologies Pvt Ltd",
    ("88 Outer Ring Road, Marathahalli", "Bengaluru, Karnataka 560037"),
    gstin="29AABCB5678K1Z3", pan="AABCB5678K", contact="billing@bluewave-tech.example",
)
BUYER_BLR = Party("Kestrel Retail Pvt Ltd", ("4th Floor, Prestige Tower, Residency Road", "Bengaluru, Karnataka 560025"),
                  gstin="29AAGCK4321M1Z8")
BUYER_MUM = Party("Kestrel Retail Pvt Ltd", ("Unit 7, Kamala Mills Compound, Lower Parel", "Mumbai, Maharashtra 400013"),
                  gstin="27AAGCK4321M1Z4")

ACME_BANK = "Bank: HDFC Bank, Indiranagar Branch · A/c 50200012345678 · IFSC HDFC0001234"
BLUEWAVE_BANK = "Bank: ICICI Bank, Marathahalli Branch · A/c 002905012345 · IFSC ICIC0000029"
INTRA_STATE = [Tax("CGST @ 9%", Decimal(9)), Tax("SGST @ 9%", Decimal(9))]  # supplier and buyer both in Karnataka
INTER_STATE = [Tax("IGST @ 18%", Decimal(18))]  # goods shipped to Maharashtra

D = Decimal


def happy_path() -> InvoiceSpec:
    """INV-1001 from Acme against PO-5001, INR 50,000.00: should be APPROVED."""
    return InvoiceSpec(
        layout="letterhead", vendor=ACME, bill_to=BUYER_BLR, ship_to=BUYER_BLR,
        invoice_number="INV-1001", invoice_date="15/09/2026", due_date="15/10/2026", po_number="PO-5001",
        place_of_supply="Karnataka (29)", taxes=INTRA_STATE, bank=ACME_BANK,
        lines=[
            Line("A4 Copier Paper, 75 gsm (500 sheets)", 100, D("250.00")),
            Line("Ballpoint Pens, Blue (Box of 50)", 40, D("300.00")),
            Line("Heavy Duty Stapler", 12, D("447.74"), note="with 5000 staples per unit"),
        ],
    )


def duplicate_resend() -> InvoiceSpec:
    """The same invoice INV-1001, re-sent by the vendor as a copy a week later.

    The file differs from the original (it carries a "COPY - RESENT" banner and a note),
    so it is not caught by file hash: only by vendor identity + invoice number.
    """
    spec = happy_path()
    spec.banner = "COPY - RESENT"
    spec.extra_notes = ["Resent on 22/09/2026 at the customer's request. Original issued 15/09/2026."]
    return spec


def amount_mismatch() -> InvoiceSpec:
    """INV-1002 from Acme against PO-5001 (INR 50,000.00) for INR 55,000.00: 10% over, so NEEDS_REVIEW."""
    return InvoiceSpec(
        layout="letterhead", vendor=ACME, bill_to=BUYER_BLR, ship_to=BUYER_MUM,
        invoice_number="INV-1002", invoice_date="28/09/2026", due_date="28/10/2026", po_number="PO-5001",
        place_of_supply="Maharashtra (27)", taxes=INTER_STATE, bank=ACME_BANK,
        lines=[
            Line("Ergonomic Office Chair, Mesh Back", 10, D("3850.00")),
            Line("Filing Cabinet, 4 Drawer, Steel", 1, D("8110.17")),
        ],
    )


def missing_invoice_number() -> InvoiceSpec:
    """Bluewave against PO-5002 for INR 1,20,000.00, but the 'Bill No.' field was left blank."""
    return InvoiceSpec(
        layout="columns", vendor=BLUEWAVE, bill_to=BUYER_BLR, ship_to=BUYER_BLR,
        invoice_number=None, invoice_date="02/10/2026", due_date="01/11/2026", po_number="PO-5002",
        place_of_supply="Karnataka (29)", taxes=INTRA_STATE, bank=BLUEWAVE_BANK,
        lines=[
            Line("Cloud Server Annual Licence", 2, D("40000.00"), note="Subscription 01/10/2026 - 30/09/2027"),
            Line("Setup & Onboarding Services", 1, D("21694.92")),
        ],
    )


def scanned_original() -> InvoiceSpec:
    """INV-1003 from Acme against PO-5004 (INR 80,000.00) for INR 80,000.00; delivered as a scan."""
    return InvoiceSpec(
        layout="letterhead", vendor=ACME, bill_to=BUYER_BLR, ship_to=BUYER_MUM,
        invoice_number="INV-1003", invoice_date="05/10/2026", due_date="04/11/2026", po_number="PO-5004",
        place_of_supply="Maharashtra (27)", taxes=INTER_STATE, bank=ACME_BANK,
        lines=[
            Line("Laser Printer Toner Cartridge, Black", 24, D("2450.00")),
            Line("Paper Shredder, Cross-Cut", 1, D("8996.61")),
        ],
    )


# --- edge cases (data/samples/extra) -------------------------------------------------------------

NORTHSTAR = Party(
    "Northstar Services Pvt Ltd",
    ("21 Infantry Road", "Bengaluru, Karnataka 560001"),
    gstin="29AAFCN9876Q1Z1", pan="AAFCN9876Q", contact="finance@northstar-services.example",
)
NORTHSTAR_BANK = "Bank: Axis Bank, MG Road Branch · A/c 917020012345678 · IFSC UTIB0000009"
BUYER_US = Party("Kestrel Retail Inc", ("400 Market Street, Suite 12", "San Francisco, CA 94111, USA"),
                 gstin="N/A (overseas buyer)")


def within_tolerance() -> InvoiceSpec:
    """Acme INV-2001 against PO-5001 for INR 50,400.00: 0.8% over, inside the 1% tolerance -> APPROVED."""
    return InvoiceSpec(
        layout="letterhead", vendor=ACME, bill_to=BUYER_BLR, ship_to=BUYER_BLR,
        invoice_number="INV-2001", invoice_date="03/11/2026", due_date="03/12/2026", po_number="PO-5001",
        place_of_supply="Karnataka (29)", taxes=INTRA_STATE, bank=ACME_BANK,
        lines=[Line("A4 Copier Paper, 75 gsm (500 sheets)", 120, D("250.00")),
               Line("Magnetic Whiteboard, 6 x 4 ft", 2, D("6355.93"), note="Wall-mounted, with marker tray")],
    )


def closed_po() -> InvoiceSpec:
    """Northstar NS-2026-118 against PO-5003, which is CLOSED -> NEEDS_REVIEW (po_status)."""
    return InvoiceSpec(
        layout="columns", vendor=NORTHSTAR, bill_to=BUYER_BLR, ship_to=BUYER_BLR,
        invoice_number="NS-2026-118", invoice_date="04/11/2026", due_date="04/12/2026", po_number="PO-5003",
        place_of_supply="Karnataka (29)", taxes=INTRA_STATE, bank=NORTHSTAR_BANK,
        lines=[Line("Facility Management Services - October 2026", 1, D("63559.32"))],
    )


def vendor_mismatch() -> InvoiceSpec:
    """Bluewave BW/2026/0611 quoting PO-5001, which belongs to Acme -> NEEDS_REVIEW (vendor_match)."""
    return InvoiceSpec(
        layout="columns", vendor=BLUEWAVE, bill_to=BUYER_BLR, ship_to=BUYER_BLR,
        invoice_number="BW/2026/0611", invoice_date="05/11/2026", due_date="05/12/2026", po_number="PO-5001",
        place_of_supply="Karnataka (29)", taxes=INTRA_STATE, bank=BLUEWAVE_BANK,
        lines=[Line("Network Switch, 24-Port Managed", 4, D("10593.22"))],
    )


def unknown_po() -> InvoiceSpec:
    """Acme INV-2002 quoting PO-9999, which does not exist -> NEEDS_REVIEW (po_match)."""
    return InvoiceSpec(
        layout="letterhead", vendor=ACME, bill_to=BUYER_BLR, ship_to=BUYER_BLR,
        invoice_number="INV-2002", invoice_date="06/11/2026", due_date="06/12/2026", po_number="PO-9999",
        place_of_supply="Karnataka (29)", taxes=INTRA_STATE, bank=ACME_BANK,
        lines=[Line("Desk Lamp, LED, Adjustable", 30, D("599.00"))],
    )


def currency_mismatch() -> InvoiceSpec:
    """Acme INV-2003 billed in USD against PO-5004 (INR) -> NEEDS_REVIEW (currency_match)."""
    return InvoiceSpec(
        layout="letterhead", vendor=ACME, bill_to=BUYER_US, ship_to=BUYER_US,
        invoice_number="INV-2003", invoice_date="07/11/2026", due_date="07/12/2026", po_number="PO-5004",
        place_of_supply="Outside India (export)", taxes=[], currency="USD", bank=ACME_BANK,
        extra_notes=["Export of goods under LUT without payment of IGST."],
        lines=[Line("Laser Printer Toner Cartridge, Black", 24, D("29.50")),
               Line("Paper Shredder, Cross-Cut", 1, D("252.00"))],
    )


def arithmetic_error() -> InvoiceSpec:
    """Acme INV-2004 against PO-5004 with the IGST line misprinted, so subtotal + tax != total
    -> NEEDS_REVIEW (totals_arithmetic)."""
    return InvoiceSpec(
        layout="letterhead", vendor=ACME, bill_to=BUYER_BLR, ship_to=BUYER_MUM,
        invoice_number="INV-2004", invoice_date="08/11/2026", due_date="08/12/2026", po_number="PO-5004",
        place_of_supply="Maharashtra (27)", taxes=INTER_STATE, bank=ACME_BANK, misprinted_tax=D("12000.00"),
        lines=[Line("Laser Printer Toner Cartridge, Black", 24, D("2450.00")),
               Line("Paper Shredder, Cross-Cut", 1, D("8996.61"))],
    )
