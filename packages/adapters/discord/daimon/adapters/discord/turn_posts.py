"""Record what a turn posts, so its agent can tidy it later with the tidy tools,
and carry out an archive of the turn's own thread once the turn is over.

The status card, answer chunks and in-thread notices a mention or continuation
turn sends through `sender`, and the thread opened from a mention, get an
`agent_posted_messages` row naming the turn's agent and the person who asked
(`daimon.core.channel_tidy.record_turn_post`). Turn error notices
(`_render_turn_error`), setup-wizard turns and session-output files are not
recorded yet, so they cannot be tidied with the tools.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from daimon.adapters.discord.lifecycle import SendFn
from daimon.core.channel_tidy import record_turn_post
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger(__name__)

_PLATFORM = "discord"


@dataclass(frozen=True)
class TurnPostRecorder:
    """One turn's agent and requester, for recording what that turn posts."""

    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    ma_agent_id: str
    requester_id: int

    async def message(
        self, thread: discord.Thread, sent: discord.Message, *, turn_card_intent_id: uuid.UUID
    ) -> None:
        await record_turn_post(
            self.sessionmaker,
            tenant_id=self.tenant_id,
            platform=_PLATFORM,
            ma_agent_id=self.ma_agent_id,
            channel_id=str(thread.id),
            message_id=str(sent.id),
            requester_platform_user_id=str(self.requester_id),
            source="turn",
            turn_card_intent_id=turn_card_intent_id,
            parent_channel_id=str(thread.parent_id),
        )

    async def opened_thread(self, thread: discord.Thread) -> None:
        await record_turn_post(
            self.sessionmaker,
            tenant_id=self.tenant_id,
            platform=_PLATFORM,
            ma_agent_id=self.ma_agent_id,
            channel_id=str(thread.parent_id),
            message_id=str(thread.id),
            requester_platform_user_id=str(self.requester_id),
            source="auto_thread",
        )

    def sender(self, thread: discord.Thread, *, turn_card_intent_id: uuid.UUID) -> SendFn:
        """`thread.send` that records each message it sends for this turn."""

        async def send(*args: Any, **kwargs: Any) -> discord.Message:  # noqa: ANN401
            sent = await thread.send(*args, **kwargs)
            await self.message(thread, sent, turn_card_intent_id=turn_card_intent_id)
            return sent

        return send


async def archive_thread_quietly(thread: discord.Thread) -> None:
    """Archive a thread the agent asked to archive during its turn, after the turn.

    `archive_thread` on the thread a turn runs in only records the request on
    the turn's origin (an edit in an archived thread fails, so the turn could
    not finish its card). A failure is logged: the turn already answered, and
    the thread simply stays open.
    """
    try:
        await thread.edit(archived=True)
        log.info("turn.thread_archived_on_request", thread_id=thread.id)
    except Exception as exc:  # the turn is done; never fail it over the archive
        log.warning(
            "turn.thread_archive_failed", thread_id=thread.id, error_type=type(exc).__name__
        )
