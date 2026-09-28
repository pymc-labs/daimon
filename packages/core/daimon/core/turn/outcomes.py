"""Best-effort terminal observations, with bounded background I/O off the turn path."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version

import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.context_prompt import TurnContext
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

    def note_usage(self, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        if self.session_id is not None:
            key = (self.session_id, event.id)
            self._usage[key] = {"session_id": key[0], "event_id": key[1]}

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
