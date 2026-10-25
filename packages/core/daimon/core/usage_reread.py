"""Read OpenAI turn usage again when it was recorded before OpenAI reported it.

A conversation-only turn's usage is read once, at the turn's end
(`OpenAIHostIO.replay_usage`). OpenAI's usage is best-effort: it "can be null when
unknown" and "recorded counts may change" (Agents API observability). A turn read
while still null was recorded `completeness=unknown` and nothing read it again, so
it stayed pending forever and its fleet worker kept its whole hold (staging
7aba3054: about 25 finished workers, 5 of them on a customer tenant).

This pass lists such a session's turns again and, once OpenAI reports a measured
meter, records the next revision through the live path (`record_observation_usage`),
which applies it at once; the stale revision it supersedes is closed in the same
transaction.

Bounds: OpenAI conversation-only bindings only (no infrastructure charge, so a
measured revision settles); the newest revision unknown or partial; first written
at least REREAD_AFTER and at most REREAD_MAX_AGE ago; REREAD_BATCH rows per pass. A
provider read that fails leaves the row as it is, for the next pass. Past the max
age a row is no longer read; it is logged once (`accounting.usage_reread_expired`)
and keeps its hold: nothing here releases money on age.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial

import structlog
from daimon.core.accounting_outbox import apply_usage_outbox, record_observation_usage
from daimon.core.infrastructure_cost import known_infrastructure_usd
from daimon.core.pricing import provider_prices, unreported_cache_writes
from daimon.core.stores import mux_state
from mux.contracts.ids import ResourceRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.profiles import CONVERSATION_ONLY
from mux.state.usage_ledger import OutboxRow
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: OpenAI may still be filling a turn's usage in just after it ends.
REREAD_AFTER = timedelta(minutes=5)
#: A turn OpenAI has not metered within a day is not read again (logged, still held).
REREAD_MAX_AGE = timedelta(hours=24)
#: Rows one pass reads again; the oldest first, so none is starved.
REREAD_BATCH = 20

_PROFILES = (CONVERSATION_ONLY.profile_id,)

#: Lists one native session's turn usage. `revision` gives the revision to stamp on
#: an observation id. Raises on any provider failure.
ListUsage = Callable[[ResourceRef, Callable[[str], int]], Awaitable[Sequence[UsageObservation]]]


async def reread_unmeasured_usage(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    markup: Decimal,
    list_usage: ListUsage,
    now: datetime | None = None,
    interval: timedelta = timedelta(minutes=1),
    limit: int = REREAD_BATCH,
) -> int:
    """Record what OpenAI now meters for turns recorded unmetered; returns how many.

    `interval` is the caller's pass period: a row that crossed the max age since the
    previous pass is logged once.
    """
    now = now or datetime.now(UTC)
    async with sessionmaker() as db:
        rows = await mux_state.unmeasured_outbox_for_profiles(
            db,
            _PROFILES,
            created_after=now - REREAD_MAX_AGE,
            created_before=now - REREAD_AFTER,
            limit=limit,
        )
        expired = await mux_state.unmeasured_outbox_for_profiles(
            db,
            _PROFILES,
            created_after=now - REREAD_MAX_AGE - interval,
            created_before=now - REREAD_MAX_AGE,
            limit=limit,
        )
    for row, tenant_id, _ in expired:
        log.warning(
            "accounting.usage_reread_expired",
            tenant_id=str(tenant_id),
            binding_id=row.binding_id,
            observation_id=row.observation_id,
            revision=row.revision,
            completeness=row.observation.completeness,
        )
    by_session: dict[ResourceRef, list[tuple[OutboxRow, uuid.UUID, ProviderBinding]]] = {}
    for row, tenant_id, binding in rows:
        by_session.setdefault(row.observation.session, []).append((row, tenant_id, binding))
    recorded = estimated = 0
    for native, members in by_session.items():
        wanted: Mapping[str, int] = {row.observation_id: row.revision for row, _, _ in members}
        try:
            listed = await list_usage(native, partial(_next_revision, wanted))
        except Exception as error:  # a failed read leaves the rows for the next pass
            log.warning(
                "accounting.usage_reread_failed",
                session_id=native.id,
                error_class=type(error).__name__,
            )
            continue
        current = {observation.id: observation for observation in listed}
        for row, tenant_id, binding in members:
            fresh = current.get(row.observation_id)
            if fresh is None or fresh.completeness != "measured":
                continue
            stale = row.observation
            observation = fresh.model_copy(
                update={"model": stale.model, "revision": row.revision + 1}
            )
            price = (
                provider_prices(binding.profile).get((native.provider, stale.model.id))
                if stale.model is not None
                else None
            )
            infrastructure = known_infrastructure_usd(binding.profile)
            try:
                async with sessionmaker.begin() as db:
                    settled = await record_observation_usage(
                        db,
                        binding_id=row.binding_id,
                        observation=observation,
                        tenant_id=tenant_id,
                        platform_user_id=None,
                        pricing=None,
                        markup=markup,
                        channel_id=binding.thread.channel.channel_id,
                        billing_grain=stale.grain,
                        provider_price=price,
                        infrastructure_usd=infrastructure,
                    )
                    if settled:
                        # The measured revision carries the turn's whole cost; the
                        # unmetered one it supersedes only closes.
                        await apply_usage_outbox(
                            db,
                            row,
                            tenant_id=tenant_id,
                            platform_user_id=None,
                            pricing=None,
                            markup=markup,
                            channel_id=binding.thread.channel.channel_id,
                            billing_grain=stale.grain,
                            provider_price=price,
                            infrastructure_usd=infrastructure,
                        )
            except Exception as error:  # one bad row must not stop the rest
                log.warning(
                    "accounting.usage_reread_row_failed",
                    binding_id=row.binding_id,
                    observation_id=row.observation_id,
                    error_class=type(error).__name__,
                )
                continue
            recorded += 1
            estimated += price is not None and unreported_cache_writes(observation, price)
    if rows or expired:
        log.info(
            "accounting.usage_reread",
            seen=len(rows),
            recorded=recorded,
            estimated=estimated,
            expired=len(expired),
        )
    return recorded


def _next_revision(stored: Mapping[str, int], observation_id: str) -> int:
    return stored.get(observation_id, 0) + 1


def openai_usage_lister(api_key: str | None = None) -> ListUsage | None:
    """Lists usage on the install's OpenAI key, as the code-default deployment does
    (no project header); None when this process has no key. Sessions of another
    project (a manifest deployment) are refused by the driver's own scope check."""
    key = (api_key if api_key is not None else os.environ.get(_key_env()) or "").strip()
    if not key:
        return None
    from daimon.core.mux_backend import platform_scope
    from daimon.core.turn.runtime_defaults import DEFAULT_PROJECT
    from mux.drivers.openai import Context, OpenAIUsage, configured_transport

    context = Context(
        configured_transport(api_key=key, project=None),
        DEFAULT_PROJECT,
        CONVERSATION_ONLY.profile_id,
        None,
    )
    scope = platform_scope("usage-reread")

    async def list_usage(
        session: ResourceRef, revision: Callable[[str], int]
    ) -> Sequence[UsageObservation]:
        class _Next:
            async def revise(
                self, session: ResourceRef, observation_id: str, meter: Mapping[str, JsonValue]
            ) -> int:
                return revision(observation_id)

        return await OpenAIUsage(context, _Next()).reconcile(scope, session)

    return list_usage


def _key_env() -> str:
    from daimon.core.provider_keys import DEFAULT_KEY_ENV

    return DEFAULT_KEY_ENV[CONVERSATION_ONLY.provider]


async def run_usage_reread_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    markup: Decimal,
    should_stop: Callable[[], bool],
    interval_s: float = 60.0,
) -> None:
    """Read unmetered OpenAI turns again until stopped; quiet in a process with no key."""
    list_usage = openai_usage_lister()
    if list_usage is None:
        return
    while not should_stop():
        try:
            await reread_unmeasured_usage(
                sessionmaker,
                markup=markup,
                list_usage=list_usage,
                interval=timedelta(seconds=interval_s),
            )
        except Exception:
            log.exception("accounting.usage_reread_pass_failed")
        await asyncio.sleep(interval_s)
