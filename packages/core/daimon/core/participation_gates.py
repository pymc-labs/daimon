"""Organic thread participation, shared shell: the gates every unmentioned burst passes.

Each adapter that follows threads gathers its own inputs (the thread and
channel ids, the burst, the window before it) and hands them here; the
cascade read, the hourly ledger, the balance, cap and channel budget gates,
the metered classifier call and the ledger write are the same on every
platform. The decisions themselves are pure (`daimon.core.thread_participation`).

`resolve` is the hot path: every unmentioned message pays for it, so it is one
indexed read. `should_respond` runs once per quiet burst, for followed threads only.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Protocol

import structlog
from anthropic import AsyncAnthropic
from daimon.core.billing import BillingConfig, is_over_cap
from daimon.core.channel_budget import is_over_channel_budget
from daimon.core.channel_isolation import is_thread_turn_refused
from daimon.core.config import ThreadParticipationSettings
from daimon.core.pricing import MODEL_PRICING
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.thread_participation import (
    count_auto_responses_since,
    get_participation_modes,
    record_auto_response,
)
from daimon.core.tenant_balance import is_over_balance
from daimon.core.thread_classifier import ClassifierOutcome, classify
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

log = structlog.get_logger()

RATE_LIMIT_WINDOW = timedelta(hours=1)
# A quiet-timer batch keeps only this many newest messages (the classifier
# window is the same size), and stops restarting its timer once it has waited
# this many quiet periods, so a thread that never goes quiet is still judged on
# a bounded delay with a bounded prompt.
BATCH_MAX_MESSAGES: Final[int] = 10
BATCH_MAX_QUIET_PERIODS: Final[int] = 6


class Classify(Protocol):
    """`daimon.core.thread_classifier.classify`'s signature, so a caller can supply its own."""

    async def __call__(
        self,
        anthropic: AsyncAnthropic,
        *,
        model: str,
        bot_display_name: str,
        recent: list[ClassifierMessage],
        candidates: list[ClassifierMessage],
    ) -> ClassifierOutcome: ...


class ParticipationGates:
    """One platform's participation gates over the shared store, billing and classifier."""

    def __init__(
        self,
        *,
        platform: str,
        settings: ThreadParticipationSettings,
        sessionmaker: async_sessionmaker[AsyncSession],
        anthropic: AsyncAnthropic,
        bot_display_name: str,
        billing_config: BillingConfig | None,
        markup: Decimal,
        deployment_default: DeploymentDefault | None = None,
    ) -> None:
        self._platform = platform
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._anthropic = anthropic
        self._bot_display_name = bot_display_name
        self._billing_config = billing_config
        self._markup = markup
        self._deployment_default = deployment_default

    async def resolve(
        self, *, tenant_id: uuid.UUID, channel_id: str, thread_id: str
    ) -> ResolvedParticipation:
        """Walk the scope cascade for this thread. One read covers all three tiers."""
        async with self._sessionmaker() as session:
            modes = await get_participation_modes(
                session,
                tenant_id=tenant_id,
                platform=self._platform,
                channel_id=channel_id,
                thread_id=thread_id,
            )
        return resolve_participation(
            deployment=self._settings.mode,
            workspace=modes.workspace,
            channel=modes.channel,
            thread=modes.thread,
        )

    async def should_respond(
        self,
        *,
        tenant_id: uuid.UUID,
        channel_id: str,
        thread_id: str,
        caller_id: str,
        candidates: Sequence[ClassifierMessage],
        recent: Callable[[], Awaitable[list[ClassifierMessage]]],
        resolved: ResolvedParticipation,
        classify: Classify = classify,
    ) -> bool:
        """Run the gates and, if they pass, the classifier. Logs every skip with its reason.

        The balance, cap and channel budget gates run before the classifier, on
        `caller_id`, the person the turn would run as, and with a deployment
        default so do the pin and isolation ones: a tenant that could not
        be admitted must not pay for the question of whether to admit it. The
        call that does run is metered to the tenant like any other model spend.
        `recent` is read only once every gate has passed.
        """
        async with self._sessionmaker() as session:
            in_window = await count_auto_responses_since(
                session,
                tenant_id=tenant_id,
                platform=self._platform,
                thread_id=thread_id,
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
                thread_id=thread_id,
                tier=resolved.tier,
            )
            return False
        if await is_over_balance(sessionmaker=self._sessionmaker, tenant_id=tenant_id):
            log.info("thread_participation.skipped", reason="over_balance", thread_id=thread_id)
            return False
        if await is_over_cap(
            billing_config=self._billing_config,
            sessionmaker=self._sessionmaker,
            tenant_id=tenant_id,
            user_id=caller_id,
            now=datetime.now(UTC),
        ):
            log.info("thread_participation.skipped", reason="over_cap", thread_id=thread_id)
            return False
        if await is_over_channel_budget(
            sessionmaker=self._sessionmaker,
            tenant_id=tenant_id,
            platform=self._platform,
            channel_id=channel_id,
            now=datetime.now(UTC),
        ):
            log.info(
                "thread_participation.skipped", reason="over_channel_budget", thread_id=thread_id
            )
            return False
        if self._deployment_default is not None and await is_thread_turn_refused(
            self._sessionmaker,
            self._anthropic,
            tenant_id=tenant_id,
            platform=self._platform,
            channel_id=channel_id,
            thread_id=thread_id,
            default=self._deployment_default,
        ):
            log.info("thread_participation.skipped", reason="agent_refused", thread_id=thread_id)
            return False
        outcome = await classify(
            self._anthropic,
            model=self._settings.classifier_model,
            bot_display_name=self._bot_display_name,
            recent=await recent(),
            candidates=list(candidates),
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
                channel_id=channel_id,
            )
        verdict = outcome.verdict
        post = decide_post_classifier(verdict)
        skipped = isinstance(post, Skip)
        log.info(
            "thread_participation.skipped" if skipped else "thread_participation.triggered",
            reason=post.reason.value if isinstance(post, Skip) else None,
            classifier_reason=verdict.reason,
            thread_id=thread_id,
            tier=resolved.tier,
        )
        return not skipped

    async def record(self, *, tenant_id: uuid.UUID, thread_id: str, message_id: str) -> None:
        """One ledger row per admitted unprompted turn, for the hourly backstop."""
        async with self._sessionmaker() as session, session.begin():
            await record_auto_response(
                session,
                tenant_id=tenant_id,
                platform=self._platform,
                thread_id=thread_id,
                message_id=message_id,
            )
