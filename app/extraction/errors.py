"""Extraction failures. Each carries a stable `code` so the API can report it."""


class ExtractionError(Exception):
    code = "extraction_failed"


class InvalidPDFError(ExtractionError):
    """The file is not a readable PDF (corrupt, truncated, encrypted, not a PDF)."""

    code = "invalid_pdf"


class TooManyPagesError(ExtractionError):
    code = "too_many_pages"


class NoTextError(ExtractionError):
    """No text could be obtained from any page, even after OCR."""

    code = "no_text"


class OCRUnavailableError(ExtractionError):
    """A page needs OCR but Tesseract (or its Python bindings) cannot be used."""

    code = "ocr_unavailable"


class OCRFailedError(ExtractionError):
    code = "ocr_failed"
