"""MCP HTTP route that streams a Slack file to the agent.

The Slack adapter hands the agent a signed URL to ``/slack/file/{token}``; this
route verifies the token, looks up and decrypts the workspace bot token, fetches
a fresh private-download URL from Slack, and streams the bytes back. This is how
Slack files (whose ``url_private`` needs the bot token) become agent-fetchable
without making them public.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.github_credentials import decrypt_token
from daimon.core.slack_file_token import verify_file_token
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

FileFetcher = Callable[[str, str], Awaitable[tuple[bytes, str, str]]]

SLACK_FILES_INFO_URL = "https://slack.com/api/files.info"


async def fetch_slack_file(
    http_client: httpx.AsyncClient, *, bot_token: str, file_id: str
) -> tuple[bytes, str, str]:
    """Return ``(bytes, content_type, filename)`` for a Slack file (auth'd).

    Raises ``httpx.HTTPError`` for every upstream failure — transport error,
    error status, non-JSON body, ``ok: false``, or a file with no download URL
    (e.g. external-mode files) — so the proxy route's ``except httpx.HTTPError``
    boundary maps all of them to a 502 rather than leaking a 500.
    """
    headers = {"Authorization": f"Bearer {bot_token}"}
    info = await http_client.get(SLACK_FILES_INFO_URL, params={"file": file_id}, headers=headers)
    info.raise_for_status()
    try:
        data: dict[str, Any] = info.json()
    except ValueError as err:  # json.JSONDecodeError subclasses ValueError
        raise httpx.HTTPError(f"files.info returned a non-JSON body: {err}") from err
    if not data.get("ok"):
        raise httpx.HTTPError(f"files.info not ok: {data.get('error', 'unknown')}")
    file_obj: dict[str, Any] = data.get("file") or {}
    download_url = file_obj.get("url_private_download")
    if not download_url:
        raise httpx.HTTPError("files.info response missing url_private_download")
    download = await http_client.get(download_url, headers=headers)
    download.raise_for_status()
    return (
        download.content,
        str(file_obj.get("mimetype", "application/octet-stream")),
        str(file_obj.get("name", "file")),
    )


def build_slack_file_proxy_route(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    secret: str,
    fetch_file: FileFetcher,
    now: Callable[[], int] = lambda: int(time.time()),
) -> Callable[[Request], Awaitable[Response]]:
    """Wire the ``/slack/file/{token}`` handler with its dependencies.

    The handler is the catch boundary: every invalid token → 403, missing token
    row → 404, upstream Slack failure → 502.
    """

    async def handler(request: Request) -> Response:
        token = request.path_params["token"]
        ref = verify_file_token(token, secret=secret, now=now())
        if ref is None:
            return PlainTextResponse("forbidden", status_code=403)
        async with sessionmaker() as session:
            row = await get_slack_bot_token(session, team_id=ref.team_id)
        if row is None:
            return PlainTextResponse("not found", status_code=404)
        try:
            bot_token = decrypt_token(fernet, row.encrypted_token)
        except InvalidToken:
            return PlainTextResponse("bad gateway", status_code=502)
        try:
            body, content_type, filename = await fetch_file(bot_token, ref.file_id)
        except httpx.HTTPError:
            return PlainTextResponse("bad gateway", status_code=502)
        safe_filename = quote(filename)
        return StreamingResponse(
            iter([body]),
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return handler
