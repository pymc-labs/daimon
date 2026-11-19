"""Re-read an ended OpenAI turn whose usage was unknown, outside its turn.

A turn resource can report usage long after the turn ended (half an hour has been
seen). No turn waits for it: the host hands the turn here and returns. One re-check
(`reread_turn`) reads it under the thread's lease and records a new revision, at the
price its trigger carries, so the cost reaches the ledger and the daily cap like any
other usage.

Two triggers start it. The process that ran the turn re-reads it (`LateUsage`), so a
footer it posted can take the debit. The scheduler's `DurableRereads` reads turns
whose usage is still pending `TAKE_AFTER` after it was seen, so a restart or a late
answer past the hour still gets read; it gives up, with a log line, a day after the
usage was seen. Reads of both overlap only in the long tail, harmlessly: a revision
already recorded is ignored and ledger keys are idempotent. A turn whose fallback
revision was already charged keeps no pending row, so only its own process re-reads it.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

import structlog
from daimon.core.channel_backend import backend_refusal_causes

if TYPE_CHECKING:
    from daimon.core.pricing import ProviderPrice
    from daimon.core.stores.mux_state import PendingSettlement
    from mux.contracts.ids import ResourceRef, Scope
    from mux.contracts.usage import UsageObservation
    from mux.drivers.openai import OpenAIUsage
    from mux.state.lease import Slot
    from mux.state.store import StateStore
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: Waits before each in-process re-read, doubling from 2 s: over an hour in all.
DELAYS_S = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0)

#: The scheduler's waits, once it has taken a turn over.
DURABLE_DELAYS_S = (60.0, 120.0, 240.0, 480.0, 960.0, 1920.0)

#: How long one late read of a turn resource may take, lease included.
READ_TIMEOUT_S = 20.0

#: How long after its usage was seen the scheduler takes a turn: the early, dense
#: in-process re-reads are over by then.
TAKE_AFTER = timedelta(minutes=10)

#: Most turns waiting on a re-read at once, per process.
MAX_PENDING = 256

#: One re-read at `now`: True once the usage is known or can no longer be read.
type Reread = Callable[[datetime], Awaitable[bool]]


@dataclass(frozen=True, kw_only=True)
class LateTurn:
    """An ended root turn whose usage is not final, and whom its usage is charged to."""

    tenant_id: UUID
    binding_id: str
    slot: Slot
    session: ResourceRef
    turn_id: str
    model_id: str
    #: The tariff and infrastructure its trigger charges at.
    price: ProviderPrice | None
    infrastructure: Callable[[UsageObservation], Awaitable[Decimal | None]]
    #: When its usage was last seen.
    observed_at: datetime
    platform_user_id: str | None
    channel_id: str | None
    agent_id: str | None


async def reread_turn(
    turn: LateTurn,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: StateStore,
    usage: OpenAIUsage,
    scope: Scope,
    markup: Decimal,
    now: datetime,
    read_timeout_s: float = READ_TIMEOUT_S,
    durable: bool = False,
) -> bool:
    """One late read of the turn under its thread's lease, recording what it found.

    True once the turn resource itself gave final usage, or the thread has no such
    turn to read; a fallback value never ends the re-reads.
    """
    from daimon.core.turn.persistence import TurnPersistence
    from daimon.core.turn.replace_binding import hold_slot
    from daimon.core.usage_recording import record_provider_usage

    ref = turn.session
    bound = await store.get_binding(turn.slot)
    if bound is None or bound.native_refs.get("session") != ref.id:
        log.warning(
            "late_usage.rebound",
            turn_id=turn.turn_id,
            session_id=ref.id,
            bound_session_id=None if bound is None else bound.native_refs.get("session"),
            binding_id=turn.binding_id,
            durable=durable,
        )
        return True  # the thread moved to another session, or was erased
    async with hold_slot(store, turn.slot, now) as lease:
        reading = TurnPersistence(
            store, bound, scope, operation_key="late-usage:" + turn.turn_id, session_ref=ref
        )
        async with asyncio.timeout(read_timeout_s):
            with reading.holding(lease):
                values = await usage.reconcile_turn(scope, ref, turn.turn_id)
    known = [value for value in values if value.completeness != "unknown"]
    if known:  # measures how long OpenAI takes to report a turn's usage
        log.info(
            "openai_late_usage.found",
            turn_id=turn.turn_id,
            final=any(value.final for value in known),
            after_s=round((now - turn.observed_at).total_seconds(), 1),
        )
    for value in known:
        await record_provider_usage(
            sessionmaker=sessionmaker,
            binding_id=turn.binding_id,
            observation=value,
            tenant_id=turn.tenant_id,
            platform_user_id=turn.platform_user_id,
            provider_price=turn.price,
            infrastructure_usd=await turn.infrastructure(value),
            billing_grain="turn",
            managed_session_id=ref.id,
            model_id=turn.model_id,
            markup=markup,
            channel_id=turn.channel_id,
            agent_id=turn.agent_id,
        )
    return any(value.final for value in values) or not values


class LateUsage:
    """Background re-reads with bounded backoff, at most one per turn."""

    def __init__(
        self,
        *,
        delays_s: Sequence[float] = DELAYS_S,
        max_pending: int = MAX_PENDING,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._delays, self._sleep, self._clock = tuple(delays_s), sleep, clock
        self._max_pending = max_pending
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def defer(self, key: str, reread: Reread) -> None:
        """Schedule `reread` for `key` unless one is already scheduled; never waits.

        Past `max_pending` turns the new one is skipped and left unbilled.
        """
        if key in self._tasks:
            return
        if len(self._tasks) >= self._max_pending:
            log.warning("openai_late_usage.skipped", key=key, pending=len(self._tasks))
            return
        # A fresh context: nothing of the turn that scheduled it (its lease) carries over.
        task = asyncio.create_task(self._run(key, reread), context=contextvars.Context())
        self._tasks[key] = task
        task.add_done_callback(lambda _: self._tasks.pop(key, None))

    async def wait(self) -> None:
        """Until every scheduled re-read has finished."""
        while self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _run(self, key: str, reread: Reread) -> None:
        for attempt, delay in enumerate(self._delays, 1):
            await self._sleep(delay)
            try:
                if await reread(self._clock()):
                    return
            except Exception as error:  # a busy thread or failed read: retry later
                log.info(
                    "openai_late_usage.retry",
                    causes=backend_refusal_causes(error),
                    key=key,
                    attempt=attempt,
                    error_class=type(error).__name__,
                )
        log.warning("openai_late_usage.unknown", key=key, attempts=len(self._delays))


#: The process's re-reads, shared by every OpenAI runtime.
LATE_USAGE = LateUsage()


#: Pending usage seen this long ago is given up on: logged once, left pending.
GIVE_UP_AFTER = timedelta(days=1)

#: Most re-reads one scheduler pass makes, and pending rows it looks at.
PASS_READS, PASS_ROWS = 4, 200

#: Longest one pass reads for, so the tick's routines and sweeps after it are not held.
PASS_BUDGET_S = 10.0


class DurableRereads:
    """Re-reads ended OpenAI turns whose pending usage is not final, from the scheduler.

    A turn is taken once `take_after` passed since its usage was seen: until then only
    the process that ran it re-reads it. Rows are durable, so a restart only resets the
    backoff: each turn is read again after `delays_s` (the last repeating) until its
    usage is final, the thread moved, or `give_up_after` passed since the usage was
    seen. A pass stops at `budget_s`; the read it cuts off backs off like a failed one,
    so a slow read cannot hold the rows behind it.
    """

    def __init__(
        self,
        *,
        delays_s: Sequence[float] = DURABLE_DELAYS_S,
        take_after: timedelta = TAKE_AFTER,
        give_up_after: timedelta = GIVE_UP_AFTER,
        reads: int = PASS_READS,
        budget_s: float = PASS_BUDGET_S,
    ) -> None:
        self._delays, self._give_up_after, self._reads = tuple(delays_s), give_up_after, reads
        self._take_after, self._budget_s = take_after, budget_s
        self._due: dict[tuple[str, str], tuple[int, datetime]] = {}
        self._given_up: set[tuple[str, str]] = set()
        self._cursor: tuple[str, str, int] | None = None

    async def run(
        self, sessionmaker: async_sessionmaker[AsyncSession], *, markup: Decimal, now: datetime
    ) -> int:
        """One pass: the re-reads made."""
        from daimon.core.stores import mux_state
        from daimon.core.turn.hosted_codec import OPENAI

        async with sessionmaker() as db:
            rows = await mux_state.pending_outbox_for_profiles(
                db, OPENAI.profiles, limit=PASS_ROWS, after=self._cursor
            )
        last = rows[-1].row if len(rows) == PASS_ROWS else None
        self._cursor = (
            None if last is None else (last.binding_id, last.observation_id, last.revision)
        )
        seen: set[tuple[str, str]] = set()
        reads = 0
        deadline = asyncio.get_running_loop().time() + self._budget_s
        for settlement in rows:
            row, observation = settlement.row, settlement.row.observation
            key = (row.binding_id, row.observation_id)
            if key in seen or observation.final or observation.turn_id is None:
                continue
            seen.add(key)  # the newest revision comes first
            if now - observation.observed_at < self._take_after:
                continue  # the in-process re-reads come first
            if now - observation.observed_at > self._give_up_after:
                if key not in self._given_up:
                    self._given_up.add(key)
                    self._due.pop(key, None)
                    log.warning("openai_late_usage.gave_up", key=":".join(key), durable=True)
                continue
            attempt, due = self._due.get(key, (0, now))
            if due > now or reads >= self._reads:
                continue
            reads += 1
            budget = asyncio.timeout_at(deadline)
            try:
                async with budget:
                    done = await _reread(sessionmaker, settlement, markup=markup, now=now)
            except Exception as error:  # a busy thread or failed read: retry later
                log.info(
                    "openai_late_usage.retry",
                    causes=backend_refusal_causes(error),
                    key=":".join(key),
                    attempt=attempt + 1,
                    error_class=type(error).__name__,
                    durable=True,
                )
                done = False
            if done:
                self._due.pop(key, None)
            else:
                delay = self._delays[min(attempt, len(self._delays) - 1)]
                self._due[key] = (attempt + 1, now + timedelta(seconds=delay))
            if budget.expired():
                break
        return reads


async def _reread(
    sessionmaker: async_sessionmaker[AsyncSession],
    settlement: PendingSettlement,
    *,
    markup: Decimal,
    now: datetime,
) -> bool:
    """The shared re-check of a pending row's turn, read with the tenant's key."""
    from daimon.core.infrastructure_cost import known_infrastructure_usd
    from daimon.core.pricing import provider_prices
    from daimon.core.stores import mux_state
    from daimon.core.turn.openai_deployment import usage_transport
    from daimon.core.turn.openai_state import OpenAIUsageRevisions
    from mux.contracts.ids import Scope
    from mux.drivers.openai import Context, OpenAIUsage

    row, observation = settlement.row, settlement.row.observation
    ref, turn_id, model = observation.session, observation.turn_id, observation.model
    assert turn_id is not None
    transport = usage_transport(str(settlement.tenant_id), settlement.profile, ref.account_scope_id)
    if transport is None or model is None or ref.account_id is None:
        raise LookupError("no OpenAI key or model for this usage")
    async with sessionmaker() as db:
        slot = await mux_state.slot_of_binding(db, row.binding_id)
        agent_id = await mux_state.agent_of_usage_session(
            db, tenant_id=settlement.tenant_id, binding_id=row.binding_id, managed_session_id=ref.id
        )
    if slot is None:
        log.warning("late_usage.rebound", turn_id=turn_id, session_id=ref.id, durable=True)
        return True  # the thread was erased
    scope = Scope(
        tenant_id=str(settlement.tenant_id),
        account_id=ref.account_id,
        principal_id="late-usage",
        authorization_id="late-usage",
    )

    def authorized(asked: Scope, kind: str, id_: str | None) -> bool:
        return asked == scope and kind == "session" and id_ == ref.id

    async def infrastructure(value: UsageObservation) -> Decimal | None:
        return known_infrastructure_usd(settlement.profile)

    turn = LateTurn(
        tenant_id=settlement.tenant_id,
        binding_id=row.binding_id,
        slot=slot,
        session=ref,
        turn_id=turn_id,
        model_id=model.id,
        price=provider_prices(settlement.profile).get((ref.provider, model.id)),
        infrastructure=infrastructure,
        observed_at=observation.observed_at,
        platform_user_id=settlement.platform_user_id,
        channel_id=settlement.channel_id,
        agent_id=agent_id,
    )
    return await reread_turn(
        turn,
        sessionmaker=sessionmaker,
        store=mux_state.PostgresStateStore(sessionmaker),
        usage=OpenAIUsage(
            Context(transport, ref.account_scope_id, settlement.profile, authorized),
            OpenAIUsageRevisions(),
        ),
        scope=scope,
        markup=markup,
        now=now,
        durable=True,
    )


#: The scheduler's durable re-reads (one scheduler process drains).
DURABLE_REREADS = DurableRereads()
