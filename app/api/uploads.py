"""Checks on uploaded files that happen before any run is created."""

import unicodedata
from pathlib import PurePosixPath

ALLOWED_MEDIA_TYPES = {"application/pdf", "application/x-pdf", "application/octet-stream", "binary/octet-stream"}
_FORBIDDEN_CHARS = set('<>:"|?*')
_MAX_NAME_LENGTH = 200
DEFAULT_NAME = "upload.pdf"


def media_type_of(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def safe_display_name(raw: str | None) -> str:
    """The client's file name, reduced to a harmless label for display.

    Directory parts (with / or \\), control characters and characters that are unsafe
    in file names are removed. This name is shown to users only; the stored file is
    named after the run id (see app.workflow.orchestrator).
    """
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if ch.isprintable() and ch not in _FORBIDDEN_CHARS)
    name = name.strip(" .")[:_MAX_NAME_LENGTH].strip()
    return name or DEFAULT_NAME
