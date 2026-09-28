"""Best-effort terminal observations, with bounded background I/O off the turn path."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version

import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.context_prompt import TurnContext
from daimon.core.pricing import MODEL_PRICING, cost_of
from daimon.core.stores.turn_outcomes import OutcomeRecord, record
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason, termination_reason
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
_PENDING: set[asyncio.Task[None]] = set()
_MAX_PENDING = 256
_WRITE_TIMEOUT_S = 1.0
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
    usage: BetaManagedAgentsSpanModelUsage
    model_id: str | None
    cost: Decimal | None
    metered: bool


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
        token = current_outcome.set(self)
        try:
            yield
        finally:
            current_outcome.reset(token)

    usage_available: bool = True
    model_by_session: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    _samples: dict[tuple[str, str], UsageSample] = field(
        default_factory=lambda: dict[tuple[str, str], UsageSample]()
    )

    def note_usage(
        self, event: BetaManagedAgentsSpanModelRequestEndEvent, *, metered: bool = False
    ) -> None:
        if self.session_id is not None:
            key = (self.session_id, event.id)
            if key in self._usage:
                return
            self._usage[key] = {"session_id": key[0], "event_id": key[1]}
            model_id = self.model_by_session.get(self.session_id)
            cost = cost_of(event.model_usage, MODEL_PRICING.get(model_id or ""))
            self._samples[key] = UsageSample(
                event.model_usage,
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
            usage_refs=list(self._usage.values()),
            input_tokens=sum(sample.usage.input_tokens for sample in samples),
            output_tokens=sum(sample.usage.output_tokens for sample in samples),
            cache_read_input_tokens=sum(sample.usage.cache_read_input_tokens for sample in samples),
            cache_creation_input_tokens=sum(
                sample.usage.cache_creation_input_tokens for sample in samples
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
