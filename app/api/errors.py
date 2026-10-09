"""Every error leaves the API as an RFC 9457 problem document with a stable `code`."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.ingest.errors import IngestError

log = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"

_INGEST_STATUS = {
    "file_too_large": 413,
    "archive_too_large": 413,
    "empty_file": 400,
    "unsupported_format": 415,
    "xml_dtd_not_allowed": 415,
}

_HTTP_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    413: "file_too_large",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def problem(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
        "detail": detail,
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


def ingest_status(code: str) -> int:
    return _INGEST_STATUS.get(code, 422)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return problem(exc.status, exc.code, exc.detail, **exc.extra)

    @app.exception_handler(IngestError)
    async def _ingest_error(_: Request, exc: IngestError) -> JSONResponse:
        return problem(ingest_status(exc.code), exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return problem(422, "validation_error", "The request is invalid.", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        return problem(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return problem(500, "internal_error", "An unexpected error occurred.")
