"""Discord poster for the shared Managed Agents session-output sweep.

A turn's files go onto its answer: the message that carries the summary line
and the vote emoji. Each file is added by editing that message, so the answer
stays the turn's last word. A file that cannot go there (the message is full,
or the edit fails) is posted on its own below it, as before.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
# Discord's limit on attachments per message, counting the ones already there.
MAX_ATTACHMENTS_PER_MESSAGE = 10
_ATTACH_NOTICE = (
    "I couldn't attach the generated file. This bot needs the Attach Files "
    "permission in this thread."
)


@dataclass(frozen=True)
class AnswerMessage:
    """The turn's answer message the sweep attaches files to, and how to edit it.

    ``edit`` is the turn's own post transport edit, so a webhook-posted answer
    is edited through its webhook.
    """

    message_id: int
    edit: Callable[..., Awaitable[discord.Message | None]]


async def deliver_session_outputs(
    anthropic_client: AsyncAnthropic,
    thread: discord.Thread,
    *,
    session_id: str,
    may_post: Callable[[], Awaitable[bool]],
    notice_thread_ids: set[int],
    turn_window: tuple[int, int] | None = None,
    answer: AnswerMessage | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Attach session outputs to the turn's answer; leave failed uploads listed for retry.

    ``turn_window`` bounds the turn by message id: after its card, before its
    end. A file the agent already attached itself inside it, same name and
    bytes, counts as delivered and is cleared without a second copy. Files the
    session listed after the turn ended are left for the next turn's sweep.
    Without ``answer``, or when a file cannot be added to it, the file is
    posted on its own.
    """
    max_bytes = min(MAX_BYTES_PER_FILE, thread.guild.filesize_limit)
    already_posted: dict[tuple[str, int], list[discord.Attachment]] | None = None
    target = _AnswerTarget(thread, answer, session_id=session_id)

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
        if await target.attach(file.content, name):
            return
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
            created_before=(
                discord.utils.snowflake_time(turn_window[1]) if turn_window is not None else None
            ),
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


class _AnswerTarget:
    """Adds files to the answer message, one edit per file.

    One file per edit keeps each upload inside the guild's per-request size
    limit, since the attachments already on the message are kept by reference,
    not uploaded again. Discord replaces the attachment list on edit, so every
    edit passes the current attachments back. The message is fetched fresh,
    because the turn's own copy may predate its table images; after that each
    edit's returned message is the current state. Any failure turns the target
    off for the rest of the sweep, and the caller posts the file instead.
    """

    def __init__(
        self, thread: discord.Thread, answer: AnswerMessage | None, *, session_id: str
    ) -> None:
        self._thread = thread
        self._answer = answer
        self._session_id = session_id
        self._message: discord.Message | None = None

    def _give_up(self, reason: str, exc: BaseException | None = None) -> None:
        log.warning(
            "discord.output_delivery.answer_attach_failed",
            session_id=self._session_id,
            reason=reason,
            error_type=type(exc).__name__ if exc is not None else None,
        )
        self._answer = None

    async def attach(self, content: bytes, filename: str) -> bool:
        """Whether the file is now on the answer; False means post it separately."""
        answer = self._answer
        if answer is None:
            return False
        if self._message is None:
            try:
                self._message = await self._thread.fetch_message(answer.message_id)
            except discord.HTTPException as exc:
                self._give_up("fetch_failed", exc)
                return False
        message = self._message
        if len(message.attachments) >= MAX_ATTACHMENTS_PER_MESSAGE:
            log.info(
                "discord.output_delivery.answer_full",
                session_id=self._session_id,
                message_id=message.id,
            )
            return False
        attachments: list[discord.Attachment | discord.File] = [
            *message.attachments,
            discord.File(io.BytesIO(content), filename=filename),
        ]
        try:
            # No replacement post: a webhook answer that cannot be edited must
            # not be sent again as a second copy of the chunk.
            edited = await answer.edit(message, attachments=attachments, _allow_replacement=False)
        except (discord.HTTPException, discord.ClientException) as exc:
            self._give_up("edit_failed", exc)
            return False
        # A same-id result is the message as it now stands; anything else means
        # refetching before the next file rather than trusting a stale list.
        self._message = (
            edited if isinstance(edited, discord.Message) and edited.id == message.id else None
        )
        log.info(
            "discord.output_delivery.attached_to_answer",
            session_id=self._session_id,
            message_id=message.id,
            filename=filename,
        )
        return True


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
