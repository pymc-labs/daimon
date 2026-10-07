"""Record what a turn posts, so its agent can tidy it later with the tidy tools.

Every status card, answer and notice a turn sends into its thread, and the
thread opened from a mention, gets an `agent_posted_messages` row naming the
turn's agent and the person who asked (`daimon.core.channel_tidy.record_turn_post`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from daimon.adapters.discord.lifecycle import SendFn
from daimon.core.channel_tidy import record_turn_post
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

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
