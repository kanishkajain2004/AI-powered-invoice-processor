"""Read a PDF into positioned text lines, using OCR only for pages that need it."""

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.config import get_settings
from app.extraction import ocr
from app.extraction.errors import InvalidPDFError, NoTextError, TooManyPagesError
from app.extraction.layout import Line, Word, words_to_lines

pymupdf.TOOLS.mupdf_display_errors(False)  # report problems through our exceptions, not stderr

_LOW_OCR_CONFIDENCE = 80.0


@dataclass
class PageText:
    number: int  # 1-based
    method: str  # "text" | "ocr" | "blank"
    lines: list[Line]


@dataclass
class DocumentText:
    pages: list[PageText]
    warnings: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[Line]:
        return [line for page in self.pages for line in page.lines]

    @property
    def text(self) -> str:
        return "\n\n".join("\n".join(line.text for line in page.lines) for page in self.pages)

    @property
    def ocr_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.method == "ocr"]


def _alnum_count(words: list[Word]) -> int:
    return sum(ch.isalnum() for w in words for ch in w.text)


def _has_non_text_content(page: pymupdf.Page) -> bool:
    """True if the page shows images or vector graphics, i.e. content OCR could read."""
    return bool(page.get_images(full=False)) or bool(page.get_drawings())


def _open(data: bytes) -> pymupdf.Document:
    if not data:
        raise InvalidPDFError("The file is empty.")
    if b"%PDF-" not in data[:1024]:
        raise InvalidPDFError("The file is not a PDF (no %PDF header found).")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several types for corrupt input
        raise InvalidPDFError(f"The PDF could not be opened: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise InvalidPDFError("The PDF is password-protected; upload an unencrypted copy.")
    if doc.page_count == 0:
        doc.close()
        raise InvalidPDFError("The PDF has no pages (it may be truncated or corrupt).")
    return doc


def validate_pdf(data: bytes) -> int:
    """Check that `data` is a readable, unencrypted PDF within the page limit; return its page count.

    Raises InvalidPDFError or TooManyPagesError. Cheap: no text is extracted.
    """
    with _open(data) as doc:
        if doc.page_count > get_settings().max_pages:
            raise TooManyPagesError(
                f"The PDF has {doc.page_count} pages; the limit is {get_settings().max_pages}."
            )
        try:
            doc.load_page(0)
        except Exception as exc:
            raise InvalidPDFError(f"The PDF could not be read: {exc}") from exc
        return doc.page_count


def read_document(source: Path | str | bytes) -> DocumentText:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    settings = get_settings()

    with _open(data) as doc:
        if doc.page_count > settings.max_pages:
            raise TooManyPagesError(
                f"The PDF has {doc.page_count} pages; the limit is {settings.max_pages}."
            )
        warnings = []
        if doc.is_repaired:
            warnings.append("The PDF was damaged and has been repaired; some content may be missing.")

        pages: list[PageText] = []
        needs_ocr: list[int] = []
        try:
            for page in doc:
                words = [Word(*w[:5]) for w in page.get_text("words")]
                # A text-poor page that also shows images or drawings looks scanned. A
                # text-poor page without them is short or blank and is used as is.
                if _alnum_count(words) < settings.min_text_chars_per_page and _has_non_text_content(page):
                    pages.append(PageText(page.number + 1, "ocr", []))
                    needs_ocr.append(page.number)
                elif words:
                    pages.append(PageText(page.number + 1, "text", words_to_lines(words)))
                else:
                    pages.append(PageText(page.number + 1, "blank", []))
        except Exception as exc:
            raise InvalidPDFError(f"The PDF could not be read: {exc}") from exc

        if needs_ocr:
            tesseract = ocr.ensure_ocr_available([n + 1 for n in needs_ocr])
            for index in needs_ocr:
                words, confidence = ocr.ocr_page(doc[index], tesseract)
                pages[index].lines = words_to_lines(words)
                if confidence is not None and confidence < _LOW_OCR_CONFIDENCE:
                    warnings.append(
                        f"OCR confidence on page {index + 1} is low ({confidence:.0f}%); check extracted values."
                    )

    result = DocumentText(pages, warnings)
    if not any(ch.isalnum() for ch in result.text):
        raise NoTextError("No text was found in the PDF, even after OCR.")
    if result.ocr_pages:
        result.warnings.append(
            f"Text on page(s) {', '.join(map(str, result.ocr_pages))} was read with OCR and may contain recognition errors."
        )
    return result
