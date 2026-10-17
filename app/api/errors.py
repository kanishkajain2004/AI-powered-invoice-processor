"""Uniform JSON error responses: {"error": {"code", "message", "details"}, "run": ...}.

Unhandled exceptions are logged server-side and answered with a generic message; stack
traces and exception text never reach the client.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas import ErrorBody, ErrorResponse, RunSummary

logger = logging.getLogger(__name__)

GENERIC_INTERNAL_MESSAGE = "An unexpected error occurred. It has been logged; please try again later."

_HTTP_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "file_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
}


class ApiError(Exception):
    def __init__(
        self, status_code: int, code: str, message: str, details: Any = None, run: RunSummary | None = None
    ):
        super().__init__(message)
        self.status_code, self.code, self.message, self.details, self.run = status_code, code, message, details, run


def error_response(
    status_code: int, code: str, message: str, details: Any = None, run: RunSummary | None = None
) -> JSONResponse:
    content = ErrorResponse(error=ErrorBody(code=code, message=message, details=details), run=run).model_dump(mode="json")
    # Omit absent optional parts, but keep nulls inside the run summary (e.g. "decision": null).
    if content["run"] is None:
        del content["run"]
    if content["error"]["details"] is None:
        del content["error"]["details"]
    return JSONResponse(status_code=status_code, content=content)


def _validation_details(exc: RequestValidationError) -> list[dict]:
    # Only location, message and type: the rejected input itself is not echoed back.
    return [
        {"loc": [str(part) for part in err.get("loc", ())], "message": err.get("msg", ""), "type": err.get("type", "")}
        for err in exc.errors()
    ]


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.run)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
        return error_response(exc.status_code, _HTTP_CODES.get(exc.status_code, "http_error"), message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        return error_response(422, "validation_error", "The request is invalid.", _validation_details(exc))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return error_response(500, "internal_error", GENERIC_INTERNAL_MESSAGE)
