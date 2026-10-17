"""FastAPI entrypoint.

Local development:  uvicorn app.main:app --reload
Deployment:         sh scripts/start_api.sh   (binds 0.0.0.0 and $PORT)
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError

from app import __version__
from app.api import api_router
from app.api.errors import register_error_handlers
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.db.seed import seed_purchase_orders


def _configure_logging(level: str) -> None:
    """Send the application's own loggers (app.*) to stderr at LOG_LEVEL."""
    logger = logging.getLogger("app")
    logger.setLevel(level.upper())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


logger = logging.getLogger("app.startup")


def _seed_if_enabled() -> None:
    """Create any missing seed purchase orders. Safe to run on every start (it never overwrites)."""
    with SessionLocal() as db:
        try:
            result = seed_purchase_orders(db)
        except IntegrityError:  # another process seeded at the same moment; the data is there
            logger.info("Seed: purchase orders were created concurrently by another process")
            return
    logger.info("Seed: %d purchase order(s) created, %d already present",
                len(result.pos_created), len(result.pos_existing))


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    if settings.seed_on_startup:
        _seed_if_enabled()
    logger.info("Ready: database %s, uploads in %s", settings.database_url, settings.upload_dir)
    yield


app = FastAPI(title="Invoice Processing API", version=__version__, lifespan=lifespan)
register_error_handlers(app)
app.include_router(api_router)


@app.get("/", include_in_schema=False)
def root() -> dict:
    """A friendly answer at the bare URL (the health check itself is /api/v1/health)."""
    return {"service": "invoice-processing-api", "version": __version__, "docs": "/docs",
            "health": "/api/v1/health"}
