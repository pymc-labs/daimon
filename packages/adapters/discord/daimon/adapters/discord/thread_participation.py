"""Organic thread participation, Discord shell: decide whether an unmentioned burst gets a turn.

The decision itself is pure (`daimon.core.thread_participation`); this module
gathers its inputs -- the resolved participation mode for the thread and the
hourly ledger count -- then runs the classifier when the cheap gates pass.

Split in two on purpose. `resolve` is the hot path: every unmentioned message
in every thread pays for it, so it is one indexed read that `bot.py` uses to
drop `off` threads before it starts a timer or spends anything else.
`should_respond` runs once per quiet burst, for followed threads only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from anthropic import AsyncAnthropic
from daimon.core.billing import BillingConfig, is_over_cap
from daimon.core.config import ThreadParticipationSettings
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores.thread_participation import (
    count_auto_responses_since,
    get_participation_modes,
    record_auto_response,
)
from daimon.core.tenant_balance import is_over_balance
from daimon.core.thread_classifier import classify
from daimon.core.thread_participation import (
    ClassifierMessage,
    ParticipationSnapshot,
    ResolvedParticipation,
    Skip,
    decide_post_classifier,
    decide_pre_classifier,
    resolve_participation,
)
from daimon.core.usage_recording import record_classifier_usage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

log = structlog.get_logger()

PLATFORM = "discord"
RATE_LIMIT_WINDOW = timedelta(hours=1)


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
        self._sessionmaker = sessionmaker
        self._anthropic = anthropic
        self._bot_user_id = bot_user_id
        self._bot_display_name = bot_display_name
        self._billing_config = billing_config
        self._markup = markup

    async def resolve(
        self, *, tenant_id: uuid.UUID, thread: discord.Thread
    ) -> ResolvedParticipation:
        """Walk the scope cascade for this thread. One read covers all three tiers."""
        async with self._sessionmaker() as session:
            modes = await get_participation_modes(
                session,
                tenant_id=tenant_id,
                platform=PLATFORM,
                channel_id=str(thread.parent_id),
                thread_id=str(thread.id),
            )
        return resolve_participation(
            deployment=self._settings.mode,
            workspace=modes.workspace,
            channel=modes.channel,
            thread=modes.thread,
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
        """Run the gates and, if they pass, the classifier. Logs every skip with its reason.

        The balance and cap gates run here, before the classifier, on the
        caller the turn would run as: a tenant that could not be admitted
        must not pay for the question of whether to admit it. The call that
        does run is metered to the tenant like any other model spend.

        `trigger` is the message the turn would run as -- passed in rather
        than read off the end of `candidates`, which the caller filters by
        author and could hand over empty.
        """
        caller_id = str(trigger.author.id)
        async with self._sessionmaker() as session:
            in_window = await count_auto_responses_since(
                session,
                tenant_id=tenant_id,
                platform=PLATFORM,
                thread_id=str(thread.id),
                since=datetime.now(UTC) - RATE_LIMIT_WINDOW,
            )
        pre = decide_pre_classifier(
            ParticipationSnapshot(
                mode=resolved.mode,
                auto_responses_in_window=in_window,
                max_per_window=self._settings.max_per_hour,
            )
        )
        if isinstance(pre, Skip):
            log.info(
                "thread_participation.skipped",
                reason=pre.reason.value,
                thread_id=str(thread.id),
                tier=resolved.tier,
            )
            return False
        if await is_over_balance(sessionmaker=self._sessionmaker, tenant_id=tenant_id):
            log.info(
                "thread_participation.skipped", reason="over_balance", thread_id=str(thread.id)
            )
            return False
        if await is_over_cap(
            billing_config=self._billing_config,
            sessionmaker=self._sessionmaker,
            tenant_id=tenant_id,
            user_id=caller_id,
            now=datetime.now(UTC),
        ):
            log.info("thread_participation.skipped", reason="over_cap", thread_id=str(thread.id))
            return False
        recent = await self._recent_window(thread, exclude_ids={m.id for m in candidates})
        outcome = await classify(
            self._anthropic,
            model=self._settings.classifier_model,
            bot_display_name=self._bot_display_name,
            recent=recent,
            candidates=[
                ClassifierMessage(
                    author_name=m.author.display_name, content=m.content, is_bot=False
                )
                for m in candidates
            ],
        )
        if outcome.usage is not None:
            await record_classifier_usage(
                sessionmaker=self._sessionmaker,
                tenant_id=tenant_id,
                platform_user_id=caller_id,
                model_id=self._settings.classifier_model,
                input_tokens=outcome.usage.input_tokens,
                output_tokens=outcome.usage.output_tokens,
                cache_read_input_tokens=outcome.usage.cache_read_input_tokens,
                markup=self._markup,
                pricing=MODEL_PRICING.get(self._settings.classifier_model),
            )
        verdict = outcome.verdict
        post = decide_post_classifier(verdict)
        event = (
            "thread_participation.skipped"
            if isinstance(post, Skip)
            else "thread_participation.triggered"
        )
        log.info(
            event,
            reason=post.reason.value if isinstance(post, Skip) else None,
            classifier_reason=verdict.reason,
            thread_id=str(thread.id),
            tier=resolved.tier,
        )
        return not isinstance(post, Skip)

    async def record(self, *, tenant_id: uuid.UUID, thread_id: int, message_id: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await record_auto_response(
                session,
                tenant_id=tenant_id,
                platform=PLATFORM,
                thread_id=str(thread_id),
                message_id=message_id,
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
