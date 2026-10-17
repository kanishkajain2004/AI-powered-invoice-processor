"""Application settings, loaded from environment variables and an optional .env file."""

from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # env_ignore_empty: a variable set to "" (e.g. left blank in a hosting dashboard) means
    # "use the default", not "use an empty value".
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore", env_ignore_empty=True)

    # Storage. DATA_DIR (e.g. a mounted disk such as /var/data) places both the database and
    # the uploads in one directory; DATABASE_URL and UPLOAD_DIR override it individually.
    data_dir: Path | None = None
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'invoices.db'}"
    upload_dir: Path = PROJECT_ROOT / "data" / "uploads"
    api_base_url: str = "http://localhost:8000"  # where scripts/demo.py finds the API
    seed_on_startup: bool = False  # create the seed purchase orders at startup if missing (idempotent)

    # Extraction / OCR
    date_order: Literal["DMY", "MDY"] = "DMY"  # how to read numeric dates like 03/04/2026: "DMY" or "MDY"
    min_text_chars_per_page: int = 30  # pages with fewer alphanumeric chars are treated as scanned
    max_pages: int = 20
    max_upload_mb: int = 20
    tesseract_cmd: str | None = None  # path to the tesseract binary if it is not on PATH
    ocr_lang: str = "eng"
    ocr_dpi: int = 300

    # Validation (illustrative values)
    amount_tolerance_pct: Decimal = Decimal("1.0")  # allowed |invoice total - PO amount|, as % of the PO amount
    arithmetic_tolerance: Decimal = Decimal("0.01")  # allowed rounding difference in invoice arithmetic
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _apply_data_dir(self) -> "Settings":
        if self.data_dir is not None:
            self.data_dir = self.data_dir.expanduser().resolve()
            if "database_url" not in self.model_fields_set:
                self.database_url = f"sqlite:///{self.data_dir / 'invoices.db'}"
            if "upload_dir" not in self.model_fields_set:
                self.upload_dir = self.data_dir / "uploads"
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
