"""Document extraction: PDF -> text lines (text layer or OCR) -> ExtractionResult.

Runs fully locally with PyMuPDF and, for scanned pages, Tesseract; no AI API key
required. This stage only reads the document; business validation happens later.
"""

from pathlib import Path

from app.config import get_settings
from app.extraction.parser import parse_invoice
from app.extraction.reader import read_document
from app.schemas import ExtractionMethod, ExtractionResult


def extract_text(source: Path | str | bytes) -> str:
    """Return the document's text, one reconstructed line per row."""
    return read_document(source).text


def extract_invoice(source: Path | str | bytes) -> ExtractionResult:
    """Extract invoice fields from a PDF given as a path or raw bytes.

    Raises an ExtractionError subclass when the document cannot be read at all.
    Fields that are missing or unclear come back as None, explained in `warnings`.
    """
    document = read_document(source)
    parsed = parse_invoice(document.lines, get_settings().date_order)

    ocr_pages = document.ocr_pages
    has_text_pages = any(p.method == "text" for p in document.pages)
    if not ocr_pages:
        method = ExtractionMethod.TEXT
    else:
        method = ExtractionMethod.MIXED if has_text_pages else ExtractionMethod.OCR

    return ExtractionResult(
        invoice=parsed.invoice,
        method=method,
        page_count=len(document.pages),
        ocr_pages=ocr_pages,
        raw_text=document.text,
        raw_fields=parsed.raw_fields,
        warnings=document.warnings + parsed.warnings,
    )
