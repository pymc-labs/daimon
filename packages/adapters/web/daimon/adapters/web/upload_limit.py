"""Refuses an oversized or excess upload before anything parses it.

A multipart form is spooled to disk as it is parsed, and a route's own size check runs
after that, so the cap sits in front of it: a declared length over the cap is refused
unread, and a body without one (or lying about it) is cut off as soon as it passes the
cap. The body is read before sign-in is checked, so it is bounded three ways: a
`SpooledTemporaryFile` keeps only a small head in memory, a read that takes too long is
cut off, and at most `MAX_IN_FLIGHT` uploads run at once (more get 503). Pure ASGI, so only
the upload path is touched. Deployments also cap the body at their proxy (`docs/web.md`).
"""

from __future__ import annotations

import asyncio
import tempfile

from daimon.core.stores.file_uploads import MAX_UPLOAD_BYTES
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

UPLOAD_PATH = "/api/chat/uploads"
#: What the form's own framing (boundaries, headers) may add to the file.
FORM_OVERHEAD_BYTES = 64 * 1024
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + FORM_OVERHEAD_BYTES
#: A body stays in memory up to this, then goes to a temporary file.
SPOOL_MEMORY_BYTES = 1024 * 1024
REPLAY_CHUNK_BYTES = 64 * 1024
#: The whole body must arrive within this many seconds.
READ_TIMEOUT_SECONDS = 120.0
#: Uploads in flight at once in this process; the rest are asked to retry.
MAX_IN_FLIGHT = 8
_TOO_LARGE = {"detail": "This file is too large."}
_TOO_SLOW = {"detail": "The upload took too long."}
_BUSY = {"detail": "Too many uploads right now. Try again in a moment."}

_in_flight = 0


class _BodyTooLarge(Exception):
    pass


async def _spool(receive: Receive, spool: tempfile.SpooledTemporaryFile[bytes]) -> bool:
    """Copy the body into `spool`; False when the client went away."""
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return False
        body = message.get("body", b"")
        size += len(body)
        if size > MAX_BODY_BYTES:
            raise _BodyTooLarge
        spool.write(body)
        if not message.get("more_body", False):
            return True


class UploadLimitMiddleware:
    """413 for an upload whose body passes `MAX_BODY_BYTES`, 408 for a slow one, 503 when busy."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        global _in_flight
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != UPLOAD_PATH:
            await self._app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await JSONResponse(_TOO_LARGE, status_code=413)(scope, receive, send)
            return
        if _in_flight >= MAX_IN_FLIGHT:
            await JSONResponse(_BUSY, status_code=503, headers={"Retry-After": "5"})(
                scope, receive, send
            )
            return
        _in_flight += 1
        try:
            with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES) as spool:
                try:
                    async with asyncio.timeout(READ_TIMEOUT_SECONDS):
                        arrived = await _spool(receive, spool)
                except _BodyTooLarge:
                    await JSONResponse(_TOO_LARGE, status_code=413)(scope, receive, send)
                    return
                except TimeoutError:
                    await JSONResponse(_TOO_SLOW, status_code=408)(scope, receive, send)
                    return
                if not arrived:
                    return  # the client went away
                await self._app(scope, _replay(spool, receive), send)
        finally:
            _in_flight -= 1


def _replay(spool: tempfile.SpooledTemporaryFile[bytes], receive: Receive) -> Receive:
    """The spooled body as request messages, then the server's own (the disconnect)."""
    end = spool.seek(0, 2)
    spool.seek(0)
    done = False

    async def replayed() -> Message:
        nonlocal done
        if done:
            return await receive()
        chunk = spool.read(REPLAY_CHUNK_BYTES)
        done = spool.tell() >= end
        return {"type": "http.request", "body": chunk, "more_body": not done}

    return replayed
