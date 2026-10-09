"""Discord poster for the shared Managed Agents session-output sweep.

A turn's files go onto its answer: the message that carries the summary line
and the vote emoji. Each file is added by editing that message, so the answer
stays the turn's last word. A file that cannot go there (the message is full,
or the edit fails) is posted on its own below it, as before.
"""

from __future__ import annotations

import asyncio
import functools
import io
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

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
# How long after the turn ended a file still counts as the turn's. A listing
# entry's created_at is when Managed Agents indexed the file, about 5 s after
# the write (core output_delivery's module doc), so a file written in a turn's
# last seconds is stamped after the turn ended. Measured on staging over 154
# files: indexed a median 5.0 s after the writing tool returned, and up to
# 4.2 s after the session went idle (9.6 s for a 103 MiB archive, over the
# delivery cap anyway). A file left listed with no next turn is never
# delivered, which is worse than one landing on the previous answer. The next
# turn's start on the same session bounds it (see _turn_cutoff).
TURN_END_GRACE = timedelta(seconds=10)
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
    next_turn_start: Callable[[], datetime | None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Attach session outputs to the turn's answer; leave failed uploads listed for retry.

    ``turn_window`` bounds the turn by message id: after its card, before its
    end. A file the agent already attached itself inside it, same name and
    bytes, counts as delivered and is cleared without a second copy. Files the
    session listed more than ``TURN_END_GRACE`` after the turn ended, or after
    the next turn on the session started (``next_turn_start``, read when each
    file is checked), are left for the next turn's sweep.
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
                functools.partial(_turn_cutoff, turn_window[1], next_turn_start)
                if turn_window is not None
                else None
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


def _turn_cutoff(
    ended_before: int, next_turn_start: Callable[[], datetime | None] | None
) -> datetime:
    """When files stop being this turn's: the grace after its end, or the next turn's start.

    Inside the grace, a file can only be the next turn's once that turn has
    started, so its start bounds the grace. A file of this turn indexed after
    that goes to the next turn's answer instead, which still delivers it.
    """
    ended = discord.utils.snowflake_time(ended_before)
    cutoff = ended + TURN_END_GRACE
    started = next_turn_start() if next_turn_start is not None else None
    if started is not None and started > ended:
        cutoff = min(cutoff, started)
    return cutoff


class _AnswerTarget:
    """Adds files to the answer message, one edit per file.

    One file per edit keeps each upload inside the guild's per-request size
    limit, since the attachments already on the message are kept by reference,
    not uploaded again. Discord replaces the attachment list on edit, so every
    edit passes the current attachments back. The message is fetched fresh,
    because the turn's own copy may predate its table images; after that each
    edit's returned message is the current state. An edit whose outcome is
    unknown is checked against the refetched answer before anything else. Any
    failure turns the target off for the rest of the sweep, and the caller posts
    the file instead.
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
            except Exception as exc:  # any fetch failure: post the file instead
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
        except Exception as exc:  # classified below; a post is the fallback either way
            if _refused(exc):
                self._give_up("edit_refused", exc)
                return False
            return await self._reconcile(message, content, filename, exc)
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

    async def _reconcile(
        self, before: discord.Message, content: bytes, filename: str, exc: Exception
    ) -> bool:
        """After an edit whose outcome is unknown, whether the file landed on the answer.

        A timeout or a 5xx can arrive after Discord applied the edit. Posting
        then would deliver the file twice, and building the next edit from the
        old attachment list would drop it again. So the answer is refetched,
        the file counts as delivered when one more attachment with its name and
        size is there than before, and the next edit starts from the refetched
        message. If the answer cannot be read back, the file is posted: a
        duplicate beats a lost file.
        """
        try:
            after = await self._thread.fetch_message(before.id)
        except Exception as fetch_exc:  # unreadable: post the file instead
            self._message = None
            self._give_up("edit_unknown_refetch_failed", fetch_exc)
            return False
        self._message = after
        key = (_attachment_key(filename), len(content))
        landed = _count(after.attachments, key) > _count(before.attachments, key)
        log.warning(
            "discord.output_delivery.answer_edit_uncertain",
            session_id=self._session_id,
            message_id=before.id,
            filename=filename,
            error_type=type(exc).__name__,
            landed=landed,
        )
        if landed:
            return True
        self._give_up("edit_failed", exc)
        return False


def _refused(exc: Exception) -> bool:
    """Whether Discord, or the transport before any request, clearly refused the edit.

    A 4xx other than a timeout means the edit was not applied. Anything else
    (a timeout, a dropped connection, a 5xx) may have been applied.
    """
    if isinstance(exc, discord.ClientException):
        return True
    return isinstance(exc, discord.HTTPException) and 400 <= exc.status < 500 and exc.status != 408


def _count(attachments: list[discord.Attachment], key: tuple[str, int]) -> int:
    return sum(1 for a in attachments if (_attachment_key(a.filename), a.size) == key)


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
