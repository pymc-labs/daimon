"""Routes that move agent-produced file bytes: `/uploads/{token}` and `/helper-files/...`.

Mounted on the FastMCP-derived ASGI app via `add_route` in server.py — a
sibling of /healthz and /oauth/*, and like them it bypasses IdentityMiddleware
because the single-use upload token IS the auth.

This route exists so file bytes never transit the model's token stream: the
agent's sandbox curls the file here directly and the model only ever handles a
short handle id.

Catch at boundaries only: this module IS the catch boundary.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from daimon.core.media.filenames import sanitize_title
from daimon.core.stores.domain import UploadedFile
from daimon.core.stores.file_uploads import (
    MAX_UPLOAD_BYTES,
    UploadTooLargeError,
    create_upload,
    get_upload,
    store_upload_content,
)
from daimon.core.stores.helper_asks import MAX_FILES, add_reply_file, get_ask_by_files_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

log = structlog.get_logger(__name__)


def _too_large(request: Request) -> Response | None:
    """Refuse on the declared length before reading the body, so an oversize
    PUT costs one header rather than a full transfer into memory."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        return PlainTextResponse(
            f"upload exceeds the {MAX_UPLOAD_BYTES} byte limit", status_code=413
        )
    return None


def build_upload_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[Request], Awaitable[Response]]:
    """Build the `PUT /uploads/{token}` handler."""

    async def upload(request: Request) -> Response:
        token = request.path_params["token"]
        if (refused := _too_large(request)) is not None:
            return refused

        body = await request.body()
        async with session_factory() as session:
            try:
                row = await store_upload_content(
                    session, upload_token=token, data=body, now=datetime.now(UTC)
                )
            except UploadTooLargeError as err:
                return PlainTextResponse(str(err), status_code=413)
            if row is None:
                # Unknown, already burnt, or past TTL — deliberately one answer,
                # since the caller is unauthenticated apart from the token.
                log.info("upload.token_unusable")
                return PlainTextResponse("unknown or expired upload token", status_code=404)
            await session.commit()

        log.info("upload.stored", handle_id=row.id, bytes=len(body))
        return PlainTextResponse("", status_code=204)

    return upload


_NOT_FOUND = "unknown or expired link"


def build_helper_files_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[Request], Awaitable[Response]]:
    """Build `GET|PUT /helper-files/{token}/{name}`: one helper ask's files.

    The ask's `files_token` is the authorization, as the upload token is above.
    GET downloads a file passed to or returned by that ask (`name` is its handle
    id); PUT adds a file to the ask's reply while the helper runs (`name` is the
    filename curl appends to a URL ending in `/`).
    """

    async def download(token: str, handle_id: str) -> Response:
        async with session_factory() as session:
            ask = await get_ask_by_files_token(session, token=token, now=datetime.now(UTC))
            if ask is None or handle_id not in {f.id for f in [*ask.files, *ask.reply_files]}:
                return PlainTextResponse(_NOT_FOUND, status_code=404)
            row = await get_upload(session, tenant_id=ask.tenant_id, handle_id=handle_id)
        if row is None or row.content is None:
            return PlainTextResponse(_NOT_FOUND, status_code=404)
        return Response(
            row.content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{row.display_filename}"',
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def upload(request: Request, token: str, name: str) -> Response:
        if (refused := _too_large(request)) is not None:
            return refused
        body = await request.body()
        now = datetime.now(UTC)
        filename = sanitize_title(name)
        async with session_factory() as session:
            ask = await get_ask_by_files_token(session, token=token, now=now)
            if ask is None or ask.status != "running":
                return PlainTextResponse(_NOT_FOUND, status_code=404)
            row, upload_token = await create_upload(
                session,
                tenant_id=ask.tenant_id,
                title=filename,
                display_filename=filename,
                content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
                now=now,
            )
            try:
                await store_upload_content(session, upload_token=upload_token, data=body, now=now)
            except UploadTooLargeError as err:
                return PlainTextResponse(str(err), status_code=413)
            file = UploadedFile(id=row.id, name=filename, size=len(body))
            if not await add_reply_file(session, ask_id=ask.id, file=file, max_files=MAX_FILES):
                # Leaving without a commit drops the new upload too.
                return PlainTextResponse(
                    f"the reply holds {MAX_FILES} files already", status_code=409
                )
            await session.commit()
        log.info("helper_file.stored", handle_id=row.id, bytes=len(body))
        return PlainTextResponse(f"sent {filename}\n", status_code=201)

    async def helper_files(request: Request) -> Response:
        token, name = request.path_params["token"], request.path_params["name"]
        if request.method == "PUT":
            return await upload(request, token, name)
        return await download(token, name)

    return helper_files
