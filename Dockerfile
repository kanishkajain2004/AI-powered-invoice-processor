# Image for the FastAPI backend, with Tesseract OCR for scanned invoices.
#
# PyMuPDF (from requirements.txt) renders PDF pages to images itself, so no poppler or
# pdf2image is needed; Tesseract reads those images through pytesseract.
#
# Build:  docker build -t invoice-api .
# Run:    docker run -p 8000:8000 -e SEED_ON_STARTUP=true invoice-api
# Render runs this image and supplies $PORT.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tesseract OCR engine with English language data (the app's default OCR_LANG=eng).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --version

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Run as an unprivileged user; data (SQLite + uploads) lives in /app/data unless DATA_DIR
# points elsewhere (for example a mounted Render disk).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/uploads \
    && chown -R appuser:appuser /app/data
USER appuser

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "scripts/start_api.sh"]
