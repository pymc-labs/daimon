"""Fetch a Slack file's bytes using a workspace bot token.

HTTP-only: no DB access. Slack's `url_private` / `url_private_download` links
require the requesting bot's own token, so a caller must already hold a
decrypted bot token for the file's workspace (the MCP `/slack/file/{token}`
proxy route decrypts one from `daimon.core.stores.slack_bot_tokens`; the Slack
adapter can call this directly with its own token).
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import PurePosixPath
from typing import Any

import httpx

SLACK_FILES_INFO_URL = "https://slack.com/api/files.info"


async def fetch_slack_file(
    http_client: httpx.AsyncClient,
    *,
    bot_token: str,
    file_id: str,
    max_bytes: int | None = None,
    suffixes: Collection[str] | None = None,
) -> tuple[bytes, str, str]:
    """Return ``(bytes, content_type, filename)`` for a Slack file (auth'd).

    Raises ``httpx.HTTPError`` for every upstream failure — transport error,
    error status, non-JSON body, ``ok: false``, or a file with no download URL
    (e.g. external-mode files) — so a caller's ``except httpx.HTTPError``
    boundary maps all of them to one response rather than leaking a 500.
    `max_bytes` refuses a larger file the same way, before it is downloaded,
    and `suffixes` a file whose name ends in none of them.
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
    if max_bytes is not None and int(file_obj.get("size") or 0) > max_bytes:
        raise httpx.HTTPError(f"the file is larger than {max_bytes} bytes")
    name = str(file_obj.get("name", "file"))
    if suffixes is not None and PurePosixPath(name.lower()).suffix not in suffixes:
        raise httpx.HTTPError(f"{name} is not a {' or '.join(sorted(suffixes))} file")
    download = await http_client.get(download_url, headers=headers)
    download.raise_for_status()
    if max_bytes is not None and len(download.content) > max_bytes:
        raise httpx.HTTPError(f"the file is larger than {max_bytes} bytes")
    return (
        download.content,
        str(file_obj.get("mimetype", "application/octet-stream")),
        name,
    )
