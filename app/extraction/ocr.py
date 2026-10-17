"""OCR fallback for scanned pages, using Tesseract via pytesseract."""

import io

import pymupdf

from app.config import get_settings
from app.extraction.errors import OCRFailedError, OCRUnavailableError
from app.extraction.layout import Word

INSTALL_HINT = (
    "Install Tesseract and make sure `tesseract` is on PATH "
    "(macOS: `brew install tesseract`; Debian/Ubuntu: `sudo apt-get install tesseract-ocr`; "
    "Windows: https://github.com/UB-Mannheim/tesseract/wiki), "
    "or set TESSERACT_CMD in .env to the full path of the tesseract executable."
)


def _pages_label(pages: list[int]) -> str:
    return f"Page {pages[0]} needs" if len(pages) == 1 else f"Pages {', '.join(map(str, pages))} need"


def ensure_ocr_available(pages: list[int]):
    """Return the configured pytesseract module, or raise OCRUnavailableError explaining the fix."""
    prefix = f"{_pages_label(pages)} OCR (no usable text layer; the PDF looks scanned)"
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise OCRUnavailableError(
            f"{prefix}, but the OCR Python packages are missing ({exc.name}). "
            "Run `pip install pytesseract pillow`, then: " + INSTALL_HINT
        ) from exc

    settings = get_settings()
    if settings.tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd
    try:
        pytesseract.get_tesseract_version()
    except (pytesseract.TesseractNotFoundError, OSError) as exc:
        raise OCRUnavailableError(f"{prefix}, but the Tesseract OCR engine was not found. {INSTALL_HINT}") from exc

    installed = set(pytesseract.get_languages(config=""))
    missing = [lang for lang in settings.ocr_lang.split("+") if lang not in installed]
    if missing:
        raise OCRUnavailableError(
            f"{prefix}, but Tesseract language data for {', '.join(missing)} is not installed "
            f"(installed: {', '.join(sorted(installed)) or 'none'}). Install the language pack "
            "(e.g. `brew install tesseract-lang` or `sudo apt-get install tesseract-ocr-eng`) "
            "or change OCR_LANG."
        )
    return pytesseract


MAX_OCR_PIXELS = 40_000_000  # ~ an A2 sheet at 300 dpi; bounds memory for oversized pages
MIN_OCR_DPI = 72


def _safe_dpi(page: pymupdf.Page, dpi: int) -> int:
    """Lower the render resolution for oversized pages so a crafted PDF cannot exhaust memory."""
    area_in2 = (page.rect.width / 72) * (page.rect.height / 72)
    if area_in2 * dpi * dpi <= MAX_OCR_PIXELS:
        return dpi
    reduced = int((MAX_OCR_PIXELS / area_in2) ** 0.5)
    if reduced < MIN_OCR_DPI:
        raise OCRFailedError(
            f"Page {page.number + 1} is too large to OCR ({page.rect.width / 72:.0f} x {page.rect.height / 72:.0f} in)."
        )
    return reduced


def ocr_page(page: pymupdf.Page, pytesseract) -> tuple[list[Word], float | None]:
    """OCR one page. Returns words in PDF point coordinates and the mean word confidence (0-100)."""
    from PIL import Image

    settings = get_settings()
    dpi = _safe_dpi(page, settings.ocr_dpi)
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    image = Image.open(io.BytesIO(pix.tobytes("png")))
    try:
        data = pytesseract.image_to_data(
            image, lang=settings.ocr_lang, output_type=pytesseract.Output.DICT
        )
    except pytesseract.TesseractError as exc:
        raise OCRFailedError(f"Tesseract failed on page {page.number + 1}: {exc}") from exc

    scale = 72 / dpi
    words, confidences = [], []
    for text, left, top, width, height, conf in zip(
        data["text"], data["left"], data["top"], data["width"], data["height"], data["conf"]
    ):
        conf = float(conf)
        if not text.strip() or conf < 0:
            continue
        words.append(
            Word(left * scale, top * scale, (left + width) * scale, (top + height) * scale, text.strip())
        )
        confidences.append(conf)
    mean_conf = sum(confidences) / len(confidences) if confidences else None
    return words, mean_conf
