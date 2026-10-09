from __future__ import annotations

import json
import logging
import re
import time
import uuid
from http import HTTPStatus

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_var

log = logging.getLogger("app.access")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    """Tag each request with an ID (client-supplied or generated) and log its outcome."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = supplied if _SAFE_REQUEST_ID.match(supplied) else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            log.info("%s %s -> %d (%.1f ms)", scope["method"], scope["path"], status, elapsed)
            request_id_var.reset(token)


class BodyTooLarge(HTTPException):
    # Subclassing HTTPException matters: FastAPI turns any other exception raised while
    # reading the body into a generic 400.
    def __init__(self, limit: int) -> None:
        super().__init__(413, f"Request body exceeds the {limit:,} byte limit.")


class BodySizeLimitMiddleware:
    """Reject oversized uploads before they are buffered, not after.

    Checks Content-Length up front, and counts bytes as they arrive for chunked uploads.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _send_413(send, self.max_bytes)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge(self.max_bytes)
            return message

        await self.app(scope, limited_receive, send)


async def _send_413(send: Send, limit: int) -> None:
    body = json.dumps(
        {
            "type": "about:blank",
            "title": HTTPStatus(413).phrase,
            "status": 413,
            "code": "file_too_large",
            "detail": f"Request body exceeds the {limit:,} byte limit.",
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class TrailingSlashMiddleware:
    """Serve /api/files and /api/files/ alike, without a redirect round-trip."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path: str = scope["path"]
            if len(path) > 1 and path.endswith("/"):
                scope = dict(scope)
                scope["path"] = path.rstrip("/")
                raw = scope.get("raw_path")
                if raw:
                    scope["raw_path"] = raw.rstrip(b"/")
        await self.app(scope, receive, send)
