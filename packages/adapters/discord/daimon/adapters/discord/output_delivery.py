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
    turn_window: tuple[int, int] | None = None,
    posted: list[int] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Post session outputs into a thread; leave failed uploads listed for retry.

    A file the agent already attached itself during the turn (a post between
    the message ids in ``turn_window``), same name and bytes, counts as
    delivered: it is cleared from the listing without a second post. The ids of
    the messages this sweep posts are appended to ``posted``.
    """
    max_bytes = min(MAX_BYTES_PER_FILE, thread.guild.filesize_limit)
    already_posted: dict[tuple[str, int], list[discord.Attachment]] | None = None

    async def check_protection() -> None:
        if not await may_post():
            raise OutputPostingUnavailable("writers_none")

    async def post(file: DeliverableFile) -> None:
        await check_protection()
        name = display_filename_for(file.filename, file.mime_type)
        nonlocal already_posted
        if already_posted is None:
            already_posted = await _own_attachments(thread, window=turn_window)
        if await _same_bytes(
            already_posted.get((_attachment_key(name), file.size_bytes), []), file
        ):
            log.info(
                "discord.output_delivery.already_posted",
                session_id=session_id,
                file_id=file.file_id,
                filename=name,
                size_bytes=file.size_bytes,
            )
            return
        try:
            message = await thread.send(file=discord.File(io.BytesIO(file.content), filename=name))
        except discord.Forbidden as exc:
            raise OutputPostingUnavailable("attach_files_forbidden") from exc
        if posted is not None:
            posted.append(message.id)

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


def _attachment_key(filename: str) -> str:
    # Discord stores an attachment's name with spaces turned into underscores.
    return filename.replace(" ", "_").lower()


async def _same_bytes(candidates: list[discord.Attachment], file: DeliverableFile) -> bool:
    """Whether one of the attachments holds exactly this file's bytes."""
    for attachment in candidates:
        try:
            if await attachment.read() == file.content:
                return True
        except discord.HTTPException as exc:
            log.warning("discord.output_delivery.compare_failed", error_type=type(exc).__name__)
    return False


async def _own_attachments(
    thread: discord.Thread, *, window: tuple[int, int] | None
) -> dict[tuple[str, int], list[discord.Attachment]]:
    """Each file Daimon attached in the thread inside ``window``, by name and size."""
    if window is None:
        return {}
    me = thread.guild.me.id
    found: dict[tuple[str, int], list[discord.Attachment]] = {}
    after, before = window
    try:
        async for message in thread.history(
            limit=None, after=discord.Object(id=after), before=discord.Object(id=before)
        ):
            own = message.author.id == me or (
                message.webhook_id is not None and message.application_id == me
            )
            if own:
                for attachment in message.attachments:
                    key = (_attachment_key(attachment.filename), attachment.size)
                    found.setdefault(key, []).append(attachment)
    except discord.HTTPException as exc:
        # Unreadable history: post anyway; a duplicate beats a lost file.
        log.warning("discord.output_delivery.history_failed", error_type=type(exc).__name__)
    return found
