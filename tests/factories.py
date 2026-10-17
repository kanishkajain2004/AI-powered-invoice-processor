"""Builders for sample invoice PDFs used by tests and scripts/generate_samples.py."""

import pymupdf

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4 in points
FONT = "helv"


def _put(page: pymupdf.Page, x: float, y: float, text: str, size: float = 10, right: bool = False) -> None:
    if right:
        x -= pymupdf.get_text_length(text, fontname=FONT, fontsize=size)
    page.insert_text((x, y), text, fontname=FONT, fontsize=size)


def _pdf(draw) -> bytes:
    doc = pymupdf.open()
    draw(doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT))
    data = doc.tobytes()
    doc.close()
    return data


def acme_invoice() -> bytes:
    """Layout A: letterhead vendor (no vendor label), side-by-side "Label: value" pairs,
    serial-numbered item table with a wrapped description, single GST line."""

    def draw(page):
        _put(page, 50, 60, "Acme Office Supplies Pvt Ltd", 16)
        _put(page, 430, 60, "TAX INVOICE", 16)
        _put(page, 50, 78, "12 MG Road, Bengaluru 560001")
        _put(page, 50, 92, "GSTIN: 29AACCA1234F1Z5")

        _put(page, 50, 130, "Invoice No: INV-1001")
        _put(page, 330, 130, "Invoice Date: 15/09/2026")
        _put(page, 50, 146, "PO Number: PO-5001")
        _put(page, 330, 146, "Currency: INR")

        _put(page, 50, 180, "Bill To:")
        _put(page, 50, 194, "Zamp Demo Buyer Pvt Ltd")
        _put(page, 50, 208, "45 Residency Road, Bengaluru")

        _put(page, 50, 250, "#")
        _put(page, 75, 250, "Description")
        _put(page, 360, 250, "Qty", right=True)
        _put(page, 450, 250, "Unit Price", right=True)
        _put(page, 545, 250, "Amount", right=True)
        rows = [
            ("1", "A4 Copier Paper (500 sheets)", "100", "250.00", "25,000.00"),
            ("2", "Ballpoint Pens, Box of 50", "40", "300.00", "12,000.00"),
            ("3", "Heavy Duty Stapler", "12", "447.74", "5,372.88"),
        ]
        y = 268
        for sno, desc, qty, unit, amount in rows:
            _put(page, 50, y, sno)
            _put(page, 75, y, desc)
            _put(page, 360, y, qty, right=True)
            _put(page, 450, y, unit, right=True)
            _put(page, 545, y, amount, right=True)
            y += 18
        _put(page, 75, y - 4, "with 5000 staples")  # wrapped description of the last row

        _put(page, 380, 350, "Subtotal")
        _put(page, 545, 350, "42,372.88", right=True)
        _put(page, 380, 366, "GST @ 18%")
        _put(page, 545, 366, "7,627.12", right=True)
        _put(page, 380, 382, "Total")
        _put(page, 545, 382, "INR 50,000.00", right=True)

        _put(page, 50, 420, "Amount in words: Rupees Fifty Thousand Only")
        _put(page, 50, 436, "Thank you for your business.")

    return _pdf(draw)


def bluewave_invoice() -> bytes:
    """Layout B: labelled vendor with the value below the label, labels and values in
    separate columns, month-name date, Indian digit grouping, CGST + SGST lines."""

    def draw(page):
        _put(page, 240, 60, "TAX INVOICE", 16)

        _put(page, 50, 110, "Supplier:")
        _put(page, 330, 110, "Bill No.")
        _put(page, 420, 110, "BW/2026/0457")
        _put(page, 50, 124, "Bluewave Technologies Pvt Ltd")
        _put(page, 330, 124, "Dated")
        _put(page, 420, 124, "02 Oct 2026")
        _put(page, 50, 138, "88 Outer Ring Road, Bengaluru")
        _put(page, 330, 138, "P.O. No.")
        _put(page, 420, 138, "PO-5002")

        _put(page, 50, 170, "Buyer:")
        _put(page, 50, 184, "Zamp Demo Buyer Pvt Ltd")

        _put(page, 50, 230, "S.No")
        _put(page, 90, 230, "Particulars")
        _put(page, 360, 230, "Quantity", right=True)
        _put(page, 450, 230, "Rate", right=True)
        _put(page, 545, 230, "Amount", right=True)
        rows = [
            ("1", "Cloud Server Annual Licence", "2", "40,000.00", "80,000.00"),
            ("2", "Setup & Onboarding Services", "1", "20,000.00", "20,000.00"),
        ]
        y = 248
        for sno, desc, qty, unit, amount in rows:
            _put(page, 50, y, sno)
            _put(page, 90, y, desc)
            _put(page, 360, y, qty, right=True)
            _put(page, 450, y, unit, right=True)
            _put(page, 545, y, amount, right=True)
            y += 18

        _put(page, 360, 310, "Taxable Value")
        _put(page, 545, 310, "1,00,000.00", right=True)
        _put(page, 360, 326, "CGST @ 9%")
        _put(page, 545, 326, "9,000.00", right=True)
        _put(page, 360, 342, "SGST @ 9%")
        _put(page, 545, 342, "9,000.00", right=True)
        _put(page, 360, 358, "Grand Total")
        _put(page, 545, 358, "Rs. 1,18,000.00", right=True)

        _put(page, 50, 400, "Amount payable in Rupees.")

    return _pdf(draw)


def northstar_invoice_missing_fields() -> bytes:
    """Minimal invoice: no date, PO, currency, tax, subtotal or line items."""

    def draw(page):
        _put(page, 50, 60, "Northstar Services Pvt Ltd", 16)
        _put(page, 50, 90, "INVOICE", 14)
        _put(page, 50, 120, "Invoice Number: NS-778")
        _put(page, 50, 150, "Bill To: Zamp Demo Buyer Pvt Ltd")
        _put(page, 50, 190, "Consulting services for September")
        _put(page, 380, 230, "Total")
        _put(page, 545, 230, "75,000.00", right=True)

    return _pdf(draw)


def blank_pdf(pages: int = 1) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    data = doc.tobytes()
    doc.close()
    return data


def with_blank_page(pdf: bytes) -> bytes:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    data = doc.tobytes()
    doc.close()
    return data


def rasterize(pdf: bytes, dpi: int = 200) -> bytes:
    """Simulate a scan: each page becomes an image with no text layer."""
    src = pymupdf.open(stream=pdf, filetype="pdf")
    out = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    data = out.tobytes()
    src.close()
    out.close()
    return data


def encrypted(pdf: bytes, password: str = "secret") -> bytes:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password)
    doc.close()
    return data


def custom_invoice(
    vendor: str | None = "Acme Office Supplies Pvt Ltd",
    invoice_number: str | None = "INV-2001",
    invoice_date: str | None = "20/09/2026",
    po_number: str | None = "PO-5001",
    currency: str | None = "INR",
    subtotal: str | None = None,
    tax: str | None = None,
    total: str | None = "50,000.00",
) -> bytes:
    """A simple label/value invoice; pass None to leave a field out of the document."""

    def draw(page):
        y = 60
        if vendor:
            _put(page, 50, y, vendor, 16)
        _put(page, 430, y, "TAX INVOICE", 16)
        y = 120
        for label, value in [("Invoice No", invoice_number), ("Invoice Date", invoice_date),
                             ("PO Number", po_number), ("Currency", currency)]:
            if value is not None:
                _put(page, 50, y, f"{label}: {value}")
                y += 16
        _put(page, 50, y + 20, "Bill To:")
        _put(page, 50, y + 34, "Zamp Demo Buyer Pvt Ltd")
        y += 80
        for label, value in [("Subtotal", subtotal), ("Tax", tax), ("Total", total)]:
            if value is not None:
                _put(page, 380, y, label)
                _put(page, 545, y, value, right=True)
                y += 16

    return _pdf(draw)


def resaved(pdf: bytes) -> bytes:
    """The same document with different bytes (new metadata), as a re-exported copy would be."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    doc.set_metadata({"title": "Re-exported copy", "producer": "another tool"})
    data = doc.tobytes(garbage=3)
    doc.close()
    return data
