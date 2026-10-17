import logging

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.api.errors import ApiError
from app.db import get_db
from app.extraction.ocr import ocr_status
from app.schemas import ErrorResponse, HealthResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, responses={503: {"model": ErrorResponse}})
def health(db: Session = Depends(get_db)) -> HealthResponse:
    """Liveness, a database round trip, and whether OCR for scanned PDFs is available.

    Missing OCR does not make the service unhealthy: text PDFs still work, and scanned
    ones fail with a clear `ocr_unavailable` error.
    """
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Health check: database unreachable")
        raise ApiError(503, "database_unavailable", "The database is not reachable.") from None
    engine = ocr_status()
    return HealthResponse(status="ok", version=__version__, database="ok",
                          ocr="available" if engine else "unavailable", ocr_engine=engine)
