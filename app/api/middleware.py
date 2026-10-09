from __future__ import annotations

import json
from http import HTTPStatus

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


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
