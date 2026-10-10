"""Best-effort terminal observations, with bounded background I/O off the turn path."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version

import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.context_prompt import TurnContext
from daimon.core.pricing import (
    MODEL_PRICING,
    ProviderPrice,
    cost_of,
    provider_cost_of,
    provider_uncached_input_tokens,
    uncached_input_tokens,
)
from daimon.core.runtime_health import track_turn
from daimon.core.stores.turn_outcomes import OutcomeRecord, record
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason, termination_reason
from daimon.core.usage_aggregation import disjoint_observations, replace_observation
from daimon.core.usage_compat import event_observation
from mux.contracts.usage import UsageObservation
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
_PENDING: set[asyncio.Task[None]] = set()
_MAX_PENDING = 256
# The writer is detached from the turn, so a short checkout burst can be
# tolerated without delaying the user. Keep a bound for a wedged database.
_WRITE_TIMEOUT_S = 10.0
_RELEASE = version("daimon-core")
current_outcome: ContextVar[TurnObservation | None] = ContextVar("turn_outcome", default=None)


async def _write(sm: async_sessionmaker[AsyncSession], row: OutcomeRecord) -> None:
    try:
        # Always own a connection, even for a caller bound to a checked-out one.
        bind = sm.kw.get("bind")
        if isinstance(bind, AsyncConnection):
            sm = async_sessionmaker(bind.engine, expire_on_commit=False)
        async with asyncio.timeout(_WRITE_TIMEOUT_S):
            async with sm() as session, session.begin():
                await record(session, row)
    except Exception as exc:
        # Never log DB exception strings: SQL parameters can contain identifiers.
        log.warning(
            "turn.outcome_write_failed", turn_id=str(row.id), error_class=type(exc).__name__
        )


async def drain_outcomes() -> None:
    """Runtime shutdown/test barrier, never awaited by a turn."""
    tasks = [task for task in _PENDING if task.get_loop() is asyncio.get_running_loop()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True)
class UsageSample:
    usage: UsageObservation
    model_id: str | None
    cost: Decimal | None
    metered: bool
    provider_price: ProviderPrice | None = None


@dataclass
class TurnObservation:
    sessionmaker: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID | None
    platform: str
    channel_id: str | None = None
    thread_id: str | None = None
    origin: TurnContext = "chat"
    account_id: uuid.UUID | None = None
    agent_id: str | None = None
    session_id: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _started: float = field(default_factory=time.monotonic)
    _finished: bool = False
    _usage: dict[tuple[str, str], dict[str, str]] = field(
        default_factory=lambda: dict[tuple[str, str], dict[str, str]]()
    )

    @contextmanager
    def activate(self) -> Iterator[None]:
        nested = current_outcome.get() is self
        token = current_outcome.set(self)
        try:
            if nested:
                yield
            else:
                with track_turn(self.tenant_id):
                    yield
        finally:
            current_outcome.reset(token)

    usage_available: bool = True
    model_by_session: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    _samples: dict[tuple[str, str], UsageSample] = field(
        default_factory=lambda: dict[tuple[str, str], UsageSample]()
    )

    def note_usage(
        self,
        event: BetaManagedAgentsSpanModelRequestEndEvent | UsageObservation,
        *,
        metered: bool = False,
        provider_price: ProviderPrice | None = None,
        infrastructure_usd: Decimal | None = None,
    ) -> None:
        if isinstance(event, UsageObservation) and event.session.provider != "anthropic":
            if event.session.kind != "session" or event.session.tenant_id != (
                str(self.tenant_id) if self.tenant_id is not None else None
            ):
                raise ValueError("usage observation belongs to another tenant/session")
            if self.session_id is not None and event.session.id != self.session_id:
                raise ValueError("usage observation belongs to another session")
            values = tuple(sample.usage for sample in self._samples.values())
            updated = replace_observation(values, event)
            disjoint_observations(updated)
            if any(
                value.session == event.session
                and value.id == event.id
                and value.revision > event.revision
                for value in updated
            ):
                return
            key = (event.session.id, event.id)
            prior = self._samples.get(key)
            price = provider_price or (prior.provider_price if prior is not None else None)
            cost = (
                provider_cost_of(event, price, infrastructure_usd=infrastructure_usd)
                if price is not None
                else None
            )
            if prior is not None and prior.usage.revision == event.revision and cost is None:
                cost = prior.cost
            self._usage[key] = {"session_id": key[0], "event_id": key[1]}
            self._samples[key] = UsageSample(
                event,
                event.model.id if event.model is not None else None,
                cost,
                metered or (prior.metered if prior is not None else False),
                price,
            )
            self.usage_available = True
            return
        if self.session_id is not None:
            if isinstance(event, UsageObservation):
                if event.session.id != self.session_id:
                    raise ValueError("usage observation belongs to another session")
                if event.grain != "model_request" or event.basis != "increment":
                    raise ValueError("turn telemetry requires incremental model-request usage")
            key = (self.session_id, event.id)
            if key in self._usage:
                return
            self._usage[key] = {"session_id": key[0], "event_id": key[1]}
            model_id = self.model_by_session.get(self.session_id)
            usage = (
                event
                if isinstance(event, UsageObservation)
                else event_observation(
                    event,
                    session_id=self.session_id,
                    model_id=model_id,
                    tenant_id=str(self.tenant_id) if self.tenant_id is not None else None,
                )
            )
            if usage.model is not None:
                model_id = usage.model.id
            cost = cost_of(usage, MODEL_PRICING.get(model_id or ""))
            self._samples[key] = UsageSample(
                usage,
                model_id,
                Decimal(str(cost)) if cost is not None else None,
                metered,
            )

    def finish(
        self,
        *,
        state: TurnState | None = None,
        error: BaseException | None = None,
        reason: TerminationReason | None = None,
        recovered: bool = False,
    ) -> None:
        """Schedule once, without DB I/O or awaiting network/pool availability."""
        if self._finished:
            return
        self._finished = True
        resolved = (
            reason
            or (state.termination if state else None)
            or termination_reason(error or (state.error if state else None))
        )
        terminal_error = error or (state.error if state else None)
        samples = list(self._samples.values())
        if any(sample.usage.session.provider != "anthropic" for sample in samples):
            selected = disjoint_observations(sample.usage for sample in samples)
            identities = {(usage.session, usage.id) for usage in selected}
            samples = [
                sample
                for sample in samples
                if (sample.usage.session, sample.usage.id) in identities
            ]

        def total(getter: Callable[[UsageSample], int | None]) -> int | None:
            counts = [getter(sample) for sample in samples]
            if any(count is None for count in counts):
                return None
            return sum(count for count in counts if count is not None)

        unpriced_calls = sum(sample.cost is None for sample in samples)
        postures = {sample.metered for sample in samples}
        posture = (
            "none"
            if not postures
            else "mixed"
            if len(postures) > 1
            else "metered"
            if True in postures
            else "exempt"
        )
        row = OutcomeRecord(
            id=self.id,
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            platform=self.platform,
            channel_id=self.channel_id,
            thread_id=self.thread_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
            origin=self.origin,
            reason=resolved,
            started_at=self.started_at,
            ended_at=datetime.now(UTC),
            duration_ms=max(0, int((time.monotonic() - self._started) * 1000)),
            recovered=recovered,
            error_class=type(terminal_error).__name__ if terminal_error else None,
            release=_RELEASE,
            usage_refs=[
                self._usage[(sample.usage.session.id, sample.usage.id)] for sample in samples
            ],
            input_tokens=total(
                lambda sample: (
                    provider_uncached_input_tokens(sample.usage, sample.provider_price)
                    if sample.provider_price is not None
                    else uncached_input_tokens(sample.usage)
                )
            ),
            output_tokens=total(lambda sample: sample.usage.output_tokens),
            cache_read_input_tokens=total(lambda sample: sample.usage.input_cached_tokens),
            cache_creation_input_tokens=total(
                lambda sample: (
                    0
                    if sample.provider_price is not None
                    and sample.provider_price.cache_write is None
                    else sample.usage.input_cache_write_tokens
                )
            ),
            model_calls=len(samples),
            model_ids=sorted(
                {sample.model_id for sample in samples if sample.model_id is not None}
            ),
            cost_usd=None
            if unpriced_calls
            else sum((sample.cost or Decimal(0) for sample in samples), Decimal(0)),
            unpriced_calls=unpriced_calls,
            billing_posture=posture,
        )
        if not self.usage_available:
            # SDK polling/dispatch paths have outcomes but do not consume model spans.
            row = replace(
                row,
                input_tokens=None,
                output_tokens=None,
                cache_read_input_tokens=None,
                cache_creation_input_tokens=None,
                model_calls=None,
                model_ids=None,
                cost_usd=None,
                unpriced_calls=None,
                billing_posture=None,
            )
        if len(_PENDING) >= _MAX_PENDING:
            log.warning("turn.outcome_queue_full", turn_id=str(self.id))
            return
        task = asyncio.create_task(_write(self.sessionmaker, row), name="turn.outcome_write")
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)


@contextmanager
def observe_turn(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID | None,
    platform: str,
    channel_id: str | None = None,
    thread_id: str | None = None,
    origin: TurnContext = "chat",
) -> Iterator[TurnObservation]:
    """Whole-turn boundary, including failures between core pipeline stages."""
    observation = TurnObservation(sm, tenant_id, platform, channel_id, thread_id, origin)
    with observation.activate():
        try:
            yield observation
        except BaseException as exc:
            observation.finish(error=exc)
            raise
        finally:
            # Normal driver paths already provided their exact terminal reason.
            observation.finish(reason=TerminationReason.UNKNOWN)


def record_refusal(
    sm: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None = None,
    reason: TerminationReason = TerminationReason.ADMISSION_CONCURRENCY_SHED,
) -> None:
    observation = current_outcome.get() or TurnObservation(
        sm, tenant_id, platform, channel_id, thread_id
    )
    observation.finish(reason=reason)
