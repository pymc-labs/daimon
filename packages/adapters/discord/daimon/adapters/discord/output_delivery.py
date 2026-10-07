"""Discord poster for the shared Managed Agents session-output sweep."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable

import structlog
from anthropic import AsyncAnthropic
from daimon.core.media.filenames import display_filename_for, sanitize_title
from daimon.core.output_delivery import (
    MAX_BYTES_PER_FILE,
    DeliverableFile,
    OutputPostingUnavailable,
    SkippedFile,
    sweep_session_outputs,
)

import discord

log = structlog.get_logger(__name__)
_MIB = 1024 * 1024
_ATTACH_NOTICE = (
    "I couldn't attach the generated file. This bot needs the Attach Files "
    "permission in this thread."
)


async def deliver_session_outputs(
    anthropic_client: AsyncAnthropic,
    thread: discord.Thread,
    *,
    session_id: str,
    may_post: Callable[[], Awaitable[bool]],
    notice_thread_ids: set[int],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Post session outputs into a thread; leave failed uploads listed for retry."""
    max_bytes = min(MAX_BYTES_PER_FILE, thread.guild.filesize_limit)

    async def check_protection() -> None:
        if not await may_post():
            raise OutputPostingUnavailable("writers_none")

    async def post(file: DeliverableFile) -> None:
        await check_protection()
        name = display_filename_for(file.filename, file.mime_type)
        try:
            await thread.send(file=discord.File(io.BytesIO(file.content), filename=name))
        except discord.Forbidden as exc:
            raise OutputPostingUnavailable("attach_files_forbidden") from exc

    async def on_skip(skipped: SkippedFile) -> None:
        await check_protection()
        await thread.send(
            f"I couldn't attach `{sanitize_title(skipped.filename)}` — it is "
            f"{skipped.size_bytes / _MIB:.1f} MiB, over the "
            f"{max_bytes / _MIB:g} MiB delivery limit.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    try:
        await sweep_session_outputs(
            anthropic_client,
            session_id=session_id,
            post=post,
            on_skip=on_skip,
            sleep=sleep,
            max_bytes=max_bytes,
        )
    except OutputPostingUnavailable as exc:
        reason = str(exc)
        log.warning(
            "discord.output_delivery.unavailable",
            session_id=session_id,
            guild_id=thread.guild.id,
            reason=reason,
        )
        if reason != "attach_files_forbidden" or thread.id in notice_thread_ids:
            return
        if not await may_post():
            return
        try:
            await thread.send(_ATTACH_NOTICE, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            return
        notice_thread_ids.add(thread.id)
