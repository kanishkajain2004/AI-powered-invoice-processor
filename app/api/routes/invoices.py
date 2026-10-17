from fastapi import APIRouter, Depends, File, Response, UploadFile
from sqlalchemy.orm import Session

from app.api import presenters
from app.api.errors import ApiError
from app.api.uploads import ALLOWED_MEDIA_TYPES, media_type_of, safe_display_name
from app.config import get_settings
from app.db import get_db
from app.extraction import ExtractionError, validate_pdf
from app.schemas import ErrorResponse, RunResult, RunStatus
from app.workflow import process_invoice

router = APIRouter(prefix="/invoices", tags=["invoices"])

# HTTP status for a run that the workflow recorded as FAILED, by error code.
FAILED_RUN_STATUS = {
    "invalid_pdf": 422,
    "too_many_pages": 422,
    "no_text": 422,
    "ocr_unavailable": 503,  # the server cannot read scanned documents right now
    "ocr_failed": 500,
    "internal_error": 500,
}

_ERRORS = {code: {"model": ErrorResponse} for code in (400, 413, 415, 422, 500, 503)}


@router.post("/process", status_code=201, response_model=RunResult, responses=_ERRORS)
def process_upload(
    response: Response,
    file: UploadFile = File(..., description="The invoice, as a PDF (multipart/form-data field 'file')"),
    db: Session = Depends(get_db),
) -> RunResult:
    """Upload an invoice PDF and run the full workflow synchronously.

    * 201: the workflow completed. `decision` is APPROVED, NEEDS_REVIEW or REJECTED.
    * 400 / 413 / 415 / 422 (before processing): empty, too large, not a PDF, or an
      unreadable PDF. No run is created.
    * 422 / 500 / 503 (during processing): a run was created but FAILED. The body's
      `run` holds its run_id; the decision is null.

    The handler is a plain `def`, so FastAPI runs the blocking workflow in its thread pool.
    """
    settings = get_settings()

    media_type = media_type_of(file.content_type)
    if media_type and media_type not in ALLOWED_MEDIA_TYPES:
        raise ApiError(415, "unsupported_media_type", f"Expected a PDF (application/pdf), got '{media_type}'.")

    limit = settings.max_upload_mb * 1024 * 1024
    content = file.file.read(limit + 1)
    if not content:
        raise ApiError(400, "empty_file", "The uploaded file is empty.")
    if len(content) > limit:
        raise ApiError(413, "file_too_large", f"The file is larger than the {settings.max_upload_mb} MB limit.",
                       {"max_bytes": limit})

    try:
        validate_pdf(content)
    except ExtractionError as exc:
        raise ApiError(422, exc.code, str(exc)) from None

    run = process_invoice(db, file_name=safe_display_name(file.filename), content=content)

    if run.status is RunStatus.FAILED:
        raise ApiError(
            FAILED_RUN_STATUS.get(run.error_code, 500),
            run.error_code or "internal_error",
            presenters.public_message(run.error_code, run.error_message) or "Processing failed.",
            {"failed_stage": run.failed_stage.value if run.failed_stage else None},
            run=presenters.summary(run),
        )

    response.headers["Location"] = presenters.links(run).self
    return presenters.result(run)
