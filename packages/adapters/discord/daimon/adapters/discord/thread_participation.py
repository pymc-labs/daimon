"""Organic thread participation, Discord shell: decide whether an unmentioned burst gets a turn.

The gates are shared (`daimon.core.participation_gates`); this module maps a
`discord.Thread` onto them and reads the classifier's window from its history.

Split in two on purpose. `resolve` is the hot path: every unmentioned message
in every thread pays for it, so it is one indexed read that `bot.py` uses to
drop `off` threads before it starts a timer or spends anything else.
`should_respond` runs once per quiet burst, for followed threads only.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog
from anthropic import AsyncAnthropic
from daimon.core.billing import BillingConfig
from daimon.core.config import ThreadParticipationSettings
from daimon.core.participation_gates import ParticipationGates
from daimon.core.thread_classifier import classify
from daimon.core.thread_participation import ClassifierMessage, ResolvedParticipation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger()

PLATFORM = "discord"


class ThreadParticipant:
    def __init__(
        self,
        *,
        settings: ThreadParticipationSettings,
        sessionmaker: async_sessionmaker[AsyncSession],
        anthropic: AsyncAnthropic,
        bot_user_id: int,
        bot_display_name: str,
        billing_config: BillingConfig | None,
        markup: Decimal,
    ) -> None:
        self._settings = settings
        self._bot_user_id = bot_user_id
        self._gates = ParticipationGates(
            platform=PLATFORM,
            settings=settings,
            sessionmaker=sessionmaker,
            anthropic=anthropic,
            bot_display_name=bot_display_name,
            billing_config=billing_config,
            markup=markup,
        )

    async def resolve(
        self, *, tenant_id: uuid.UUID, thread: discord.Thread
    ) -> ResolvedParticipation:
        """Walk the scope cascade for this thread. One read covers all three tiers."""
        return await self._gates.resolve(
            tenant_id=tenant_id, channel_id=str(thread.parent_id), thread_id=str(thread.id)
        )

    async def should_respond(
        self,
        thread: discord.Thread,
        candidates: list[discord.Message],
        *,
        trigger: discord.Message,
        tenant_id: uuid.UUID,
        resolved: ResolvedParticipation,
    ) -> bool:
        """Run the shared gates and, if they pass, the classifier.

        `trigger` is the message the turn would run as -- passed in rather
        than read off the end of `candidates`, which the caller filters by
        author and could hand over empty.
        """
        exclude_ids = {m.id for m in candidates}

        async def recent() -> list[ClassifierMessage]:
            return await self._recent_window(thread, exclude_ids=exclude_ids)

        return await self._gates.should_respond(
            tenant_id=tenant_id,
            channel_id=str(thread.parent_id),
            thread_id=str(thread.id),
            caller_id=str(trigger.author.id),
            candidates=[
                ClassifierMessage(
                    author_name=m.author.display_name, content=m.content, is_bot=False
                )
                for m in candidates
            ],
            recent=recent,
            resolved=resolved,
            # Looked up here, not bound at import, so tests can swap the module's `classify`.
            classify=classify,
        )

    async def record(self, *, tenant_id: uuid.UUID, thread_id: int, message_id: str) -> None:
        await self._gates.record(
            tenant_id=tenant_id, thread_id=str(thread_id), message_id=message_id
        )

    async def _recent_window(
        self, thread: discord.Thread, *, exclude_ids: set[int]
    ) -> list[ClassifierMessage]:
        """The messages before the burst, oldest first. The burst itself is the candidates."""
        window: list[discord.Message] = []
        async for m in thread.history(
            limit=self._settings.recent_messages_window + len(exclude_ids)
        ):
            if m.id not in exclude_ids:
                window.append(m)
        window = window[: self._settings.recent_messages_window]
        window.reverse()
        return [
            ClassifierMessage(
                author_name=m.author.display_name,
                content=m.content,
                is_bot=m.author.id == self._bot_user_id,
            )
            for m in window
        ]
