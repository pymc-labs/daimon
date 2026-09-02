"""Discord send_message implementation and attachment helpers."""

from __future__ import annotations

import io
import uuid
from urllib.parse import urlparse

import aiohttp
import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._file_handles import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    resolve_file_handles,
)
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_channel,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    _resolve_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    MessageRow,
    _to_message_row,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Discord uses MiB, not MB, for the per-attachment cap.
_DISCORD_ATTACHMENT_MAX_BYTES: int = 25 * 1024 * 1024

# SSRF guard: attachment fetches are restricted to Discord's CDN hosts. A bare
# https scheme check is insufficient — an attacker-supplied https URL can
# open-redirect to an internal target (e.g. the cloud metadata endpoint). We
# allowlist the only hosts Discord serves attachments from and disable redirect
# following so an allowlisted URL cannot bounce elsewhere.
_ALLOWED_ATTACHMENT_HOSTS: frozenset[str] = frozenset(
    {"cdn.discordapp.com", "media.discordapp.net"}
)


async def _fetch_attachment(session: aiohttp.ClientSession, url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_ATTACHMENT_HOSTS:
        raise ToolError("attachment url must be an https discord cdn url")
    try:
        async with session.get(url, allow_redirects=False) as resp:
            resp.raise_for_status()
            cl = resp.content_length
            if cl is not None and cl > _DISCORD_ATTACHMENT_MAX_BYTES:
                raise ToolError("attachment exceeds 25 MiB")
            buf = io.BytesIO()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                if buf.tell() + len(chunk) > _DISCORD_ATTACHMENT_MAX_BYTES:
                    raise ToolError("attachment exceeds 25 MiB")
                buf.write(chunk)
            return buf.getvalue()
    except aiohttp.ClientError as exc:
        # Named boundary: any HTTP-fetch failure (raise_for_status, connection
        # reset, timeout) becomes a ToolError instead of escaping to FastMCP as
        # an opaque internal error. The size-limit ToolError above is not an
        # aiohttp.ClientError, so it propagates unchanged.
        raise ToolError(f"failed to fetch attachment: {exc}") from exc


async def _build_files(
    specs: list[dict[str, str]], *, session: aiohttp.ClientSession
) -> list[discord.File]:
    if len(specs) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ToolError("max 10 attachments per message")
    files: list[discord.File] = []
    for spec in specs:
        data = await _fetch_attachment(session, spec["url"])
        files.append(discord.File(fp=io.BytesIO(data), filename=spec["filename"]))
    return files


async def _build_files_from_handles(
    filenames: list[str],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> list[discord.File]:
    """Resolve file handles into discord.File attachments."""
    uploads = await resolve_file_handles(
        filenames, session_factory=session_factory, tenant_id=tenant_id
    )
    return [
        discord.File(fp=io.BytesIO(upload.content), filename=upload.filename) for upload in uploads
    ]


async def _send_message_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    content: str,
    attachments: list[dict[str, str]] | None = None,
    file_handles: list[str] | None = None,
    session: aiohttp.ClientSession | None = None,
) -> MessageRow:
    _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    token = _require_bot_token(runtime)

    total_attachments = (len(attachments) if attachments else 0) + (
        len(file_handles) if file_handles else 0
    )
    if total_attachments > MAX_ATTACHMENTS_PER_MESSAGE:
        raise ToolError("max 10 attachments per message")

    files: list[discord.File] = []
    if attachments:
        if session is not None:
            files.extend(await _build_files(attachments, session=session))
        else:
            async with aiohttp.ClientSession() as http_session:
                files.extend(await _build_files(attachments, session=http_session))
    if file_handles:
        files.extend(
            await _build_files_from_handles(
                file_handles,
                session_factory=runtime.session_factory,
                tenant_id=auth.tenant_id,
            )
        )

    async with rest_client(token) as c:
        _, member = await _resolve_member(c, guild_id, _require_discord_identity(auth))
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)
        if isinstance(channel, discord.Thread):
            # Thread.permissions_for needs the parent in the guild cache;
            # the per-call REST client starts with an empty one.
            await _ensure_thread_parent_cached(channel)
        _check_send_permission(channel, member)
        if not isinstance(channel, discord.abc.Messageable):
            raise ToolError("channel does not support sending messages")
        sent = await channel.send(content=content, files=files)
        return _to_message_row(sent)
