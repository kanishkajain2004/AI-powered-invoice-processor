"""End-to-end extraction tests on generated PDFs."""

import shutil
import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pymupdf
import pytesseract
import pytest

from app.config import get_settings
from app.extraction import (
    ExtractionError,
    InvalidPDFError,
    NoTextError,
    OCRFailedError,
    OCRUnavailableError,
    TooManyPagesError,
    extract_invoice,
    ocr,
)
from app.extraction.layout import Word
from app.schemas import ExtractedInvoice, ExtractedLineItem, ExtractionMethod
from tests import factories

D = Decimal

ACME = ExtractedInvoice(
    vendor_name="Acme Office Supplies Pvt Ltd",
    invoice_number="INV-1001",
    invoice_date=date(2026, 9, 15),
    po_number="PO-5001",
    currency="INR",
    subtotal=D("42372.88"),
    tax_amount=D("7627.12"),
    total_amount=D("50000.00"),
    line_items=[
        ExtractedLineItem(description="A4 Copier Paper (500 sheets)", quantity=D("100"), unit_price=D("250.00"), amount=D("25000.00")),
        ExtractedLineItem(description="Ballpoint Pens, Box of 50", quantity=D("40"), unit_price=D("300.00"), amount=D("12000.00")),
        ExtractedLineItem(description="Heavy Duty Stapler with 5000 staples", quantity=D("12"), unit_price=D("447.74"), amount=D("5372.88")),
    ],
)

BLUEWAVE = ExtractedInvoice(
    vendor_name="Bluewave Technologies Pvt Ltd",
    invoice_number="BW/2026/0457",
    invoice_date=date(2026, 10, 2),
    po_number="PO-5002",
    currency="INR",
    subtotal=D("100000.00"),
    tax_amount=D("18000.00"),
    total_amount=D("118000.00"),
    line_items=[
        ExtractedLineItem(description="Cloud Server Annual Licence", quantity=D("2"), unit_price=D("40000.00"), amount=D("80000.00")),
        ExtractedLineItem(description="Setup & Onboarding Services", quantity=D("1"), unit_price=D("20000.00"), amount=D("20000.00")),
    ],
)


def _words_of(pdf: bytes) -> list[list[Word]]:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return [[Word(*w[:5]) for w in page.get_text("words")] for page in doc]


@pytest.fixture
def no_tesseract(monkeypatch):
    def missing():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", missing)


@pytest.fixture
def fake_ocr(monkeypatch):
    """Replace Tesseract with a perfect 'OCR' that returns the words of a reference PDF."""

    def install(reference_pdf: bytes, confidence: float = 95.0):
        pages = _words_of(reference_pdf)
        monkeypatch.setattr(ocr, "ensure_ocr_available", lambda page_numbers: "fake-tesseract")
        monkeypatch.setattr(ocr, "ocr_page", lambda page, _engine: (pages[page.number], confidence))

    return install


# --- valid PDFs ------------------------------------------------------------------------


def test_extracts_layout_a():
    result = extract_invoice(factories.acme_invoice())

    assert result.invoice == ACME
    assert result.method is ExtractionMethod.TEXT
    assert result.page_count == 1
    assert result.ocr_pages == []
    assert result.warnings == []
    assert result.raw_fields["po_number"] == "PO Number: PO-5001"
    assert "INV-1001" in result.raw_text


def test_extracts_layout_b():
    result = extract_invoice(factories.bluewave_invoice())

    assert result.invoice == BLUEWAVE
    assert result.warnings == []
    assert result.raw_fields["tax_amount"] == "CGST @ 9%   9,000.00 | SGST @ 9%   9,000.00"


def test_accepts_path_and_str(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(factories.acme_invoice())

    assert extract_invoice(path).invoice == ACME
    assert extract_invoice(str(path)).invoice == ACME


def test_result_is_json_serialisable():
    payload = extract_invoice(factories.acme_invoice()).model_dump(mode="json")
    assert payload["invoice"]["total_amount"] == "50000.00"
    assert payload["invoice"]["invoice_date"] == "2026-09-15"


# --- missing fields --------------------------------------------------------------------


def test_missing_fields_are_none_not_guessed():
    result = extract_invoice(factories.northstar_invoice_missing_fields())
    inv = result.invoice

    assert inv.vendor_name == "Northstar Services Pvt Ltd"
    assert inv.invoice_number == "NS-778"
    assert inv.total_amount == D("75000.00")
    assert inv.invoice_date is None
    assert inv.po_number is None
    assert inv.currency is None
    assert inv.subtotal is None
    assert inv.tax_amount is None
    assert inv.line_items == []
    assert set(result.raw_fields) == {"vendor_name", "invoice_number", "total_amount"}


def test_short_text_page_does_not_need_ocr(no_tesseract):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Invoice No: A-1")
        data = doc.tobytes()

    result = extract_invoice(data)

    assert result.method is ExtractionMethod.TEXT
    assert result.invoice.invoice_number == "A-1"


def test_blank_trailing_page_does_not_need_ocr(no_tesseract):
    result = extract_invoice(factories.with_blank_page(factories.acme_invoice()))

    assert result.invoice == ACME
    assert result.page_count == 2
    assert result.method is ExtractionMethod.TEXT


# --- malformed PDFs and failures --------------------------------------------------------


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "empty"),
        (b"hello, this is not a pdf", "not a PDF"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "not a PDF"),
        (b"%PDF-1.7\n" + bytes(range(256)) * 8, "could not be opened"),
        (b"%PDF-1.7\n%%EOF", "could not be opened"),
    ],
)
def test_malformed_pdf_raises_invalid_pdf(data, message):
    with pytest.raises(InvalidPDFError, match=message) as exc_info:
        extract_invoice(data)
    assert exc_info.value.code == "invalid_pdf"


def test_truncated_pdf_is_repaired_with_warning():
    data = factories.acme_invoice()
    result = extract_invoice(data[: len(data) // 2])

    assert any("repaired" in w for w in result.warnings)


def test_encrypted_pdf_raises():
    with pytest.raises(InvalidPDFError, match="password"):
        extract_invoice(factories.encrypted(factories.acme_invoice()))


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        extract_invoice(tmp_path / "nope.pdf")


def test_blank_pdf_raises_no_text():
    with pytest.raises(NoTextError) as exc_info:
        extract_invoice(factories.blank_pdf(2))
    assert exc_info.value.code == "no_text"


def test_too_many_pages(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_pages", 1)
    with pytest.raises(TooManyPagesError, match="limit is 1"):
        extract_invoice(factories.blank_pdf(2))


def test_all_extraction_errors_share_a_base_class():
    for cls in (InvalidPDFError, NoTextError, OCRFailedError, OCRUnavailableError, TooManyPagesError):
        assert issubclass(cls, ExtractionError)


# --- OCR fallback ------------------------------------------------------------------------


def test_scanned_pdf_without_tesseract_gives_actionable_error(no_tesseract):
    scanned = factories.rasterize(factories.acme_invoice())

    with pytest.raises(OCRUnavailableError) as exc_info:
        extract_invoice(scanned)

    message = str(exc_info.value)
    assert exc_info.value.code == "ocr_unavailable"
    assert "Page 1 needs OCR" in message
    assert "brew install tesseract" in message
    assert "apt-get install tesseract-ocr" in message
    assert "TESSERACT_CMD" in message


def test_scanned_pdf_without_pytesseract_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    with pytest.raises(OCRUnavailableError, match="pip install pytesseract pillow"):
        extract_invoice(factories.rasterize(factories.acme_invoice()))


def test_missing_ocr_language_pack(monkeypatch):
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.0")
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["osd"])
    with pytest.raises(OCRUnavailableError, match="language data for eng"):
        extract_invoice(factories.rasterize(factories.acme_invoice()))


def test_tesseract_cmd_setting_is_applied(monkeypatch, no_tesseract):
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", pytesseract.pytesseract.tesseract_cmd)
    monkeypatch.setattr(get_settings(), "tesseract_cmd", "/opt/custom/tesseract")
    with pytest.raises(OCRUnavailableError):
        extract_invoice(factories.rasterize(factories.acme_invoice()))
    assert pytesseract.pytesseract.tesseract_cmd == "/opt/custom/tesseract"


def test_scanned_pdf_goes_through_ocr(fake_ocr):
    original = factories.acme_invoice()
    fake_ocr(original)

    result = extract_invoice(factories.rasterize(original))

    assert result.method is ExtractionMethod.OCR
    assert result.ocr_pages == [1]
    assert result.invoice == ACME
    assert any("read with OCR" in w for w in result.warnings)


def test_low_ocr_confidence_is_reported(fake_ocr):
    original = factories.acme_invoice()
    fake_ocr(original, confidence=55.0)

    result = extract_invoice(factories.rasterize(original))

    assert any("confidence on page 1 is low (55%)" in w for w in result.warnings)


def test_mixed_text_and_scanned_pages(fake_ocr):
    text_pdf = factories.acme_invoice()
    scanned_pdf = factories.rasterize(text_pdf)
    with pymupdf.open(stream=text_pdf, filetype="pdf") as doc, pymupdf.open(stream=scanned_pdf, filetype="pdf") as scan:
        doc.insert_pdf(scan)
        combined = doc.tobytes()
    fake_ocr(_two_page_reference(text_pdf))

    result = extract_invoice(combined)

    assert result.method is ExtractionMethod.MIXED
    assert result.ocr_pages == [2]


def _two_page_reference(text_pdf: bytes) -> bytes:
    with pymupdf.open(stream=text_pdf, filetype="pdf") as doc, pymupdf.open(stream=text_pdf, filetype="pdf") as copy:
        doc.insert_pdf(copy)
        return doc.tobytes()


def test_tesseract_error_becomes_ocr_failed(monkeypatch):
    def boom(*args, **kwargs):
        raise pytesseract.TesseractError(1, "image too small")

    engine = SimpleNamespace(image_to_data=boom, Output=pytesseract.Output, TesseractError=pytesseract.TesseractError)
    monkeypatch.setattr(ocr, "ensure_ocr_available", lambda page_numbers: engine)

    with pytest.raises(OCRFailedError, match="page 1"):
        extract_invoice(factories.rasterize(factories.acme_invoice()))


def test_ocr_page_converts_pixels_to_points(monkeypatch):
    monkeypatch.setattr(get_settings(), "ocr_dpi", 300)
    data = {
        "text": ["", "Invoice", "INV-1"],
        "left": [0, 300, 900],
        "top": [0, 600, 600],
        "width": [10, 150, 75],
        "height": [10, 30, 30],
        "conf": ["-1", "90", "70"],
    }
    engine = SimpleNamespace(image_to_data=lambda *a, **k: data, Output=pytesseract.Output, TesseractError=pytesseract.TesseractError)

    with pymupdf.open(stream=factories.blank_pdf(), filetype="pdf") as doc:
        words, confidence = ocr.ocr_page(doc[0], engine)

    assert words == [Word(72.0, 144.0, 108.0, 151.2, "Invoice"), Word(216.0, 144.0, 234.0, 151.2, "INV-1")]
    assert confidence == 80.0


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_real_ocr_on_scanned_invoice():
    result = extract_invoice(factories.rasterize(factories.acme_invoice(), dpi=200))

    assert result.method is ExtractionMethod.OCR
    inv = result.invoice
    assert inv.invoice_number == "INV-1001"
    assert inv.po_number == "PO-5001"
    assert inv.invoice_date == date(2026, 9, 15)
    assert inv.total_amount == D("50000.00")


def test_oversized_page_is_rendered_within_the_pixel_budget(monkeypatch):
    seen = {}

    def capture(image, **kwargs):
        seen["pixels"] = image.width * image.height
        return {"text": [], "left": [], "top": [], "width": [], "height": [], "conf": []}

    engine = SimpleNamespace(image_to_data=capture, Output=pytesseract.Output, TesseractError=pytesseract.TesseractError)
    with pymupdf.open() as doc:
        page = doc.new_page(width=40 * 72, height=40 * 72)  # 40 x 40 inches: 144 MP at 300 dpi
        ocr.ocr_page(page, engine)
    assert seen["pixels"] <= ocr.MAX_OCR_PIXELS


def test_absurdly_large_page_is_refused():
    engine = SimpleNamespace(image_to_data=None, Output=pytesseract.Output, TesseractError=pytesseract.TesseractError)
    with pymupdf.open() as doc:
        page = doc.new_page(width=14_000, height=14_000)  # ~194 x 194 inches
        with pytest.raises(OCRFailedError, match="too large to OCR"):
            ocr.ocr_page(page, engine)
