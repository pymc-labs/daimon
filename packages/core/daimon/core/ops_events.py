"""Report a failure or warning once: a log line for GCP and a row an admin can read.

`report_ops_event` is sync and never raises, so it is safe on any failure path. It logs a
structlog line named after the code (what the log-based metrics match), then queues the
row write the way `TurnObservation.finish` queues outcomes: detached from the caller,
bounded, drained at shutdown. Repeats of one issue within `COALESCE_S` seconds in this
process share one row whose `count` adds them up, so a bridge server that is down writes
about six rows a minute, not thousands. An event with no tenant, for example a relay
refused before an identity resolves, is logged only.

Rows carry no content: reasons are a closed vocabulary (`ops_catalog.clean_reason`) and
the only free text is a routine agent's own `note`, kept to one short line.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final

import structlog
from daimon.core.ops_catalog import (
    CATALOG,
    OpsArea,
    OpsCode,
    OpsSeverity,
    clean_note,
    clean_reason,
    fingerprint_of,
)
from daimon.core.release import current_release
from daimon.core.stores.ops_events import OpsEventRecord, record_ops_events
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: Repeats of one issue within this long share a row.
COALESCE_S: Final = 10.0
_MAX_PENDING: Final = 256
# The writer is detached from its caller; a wedged database must not hold it forever.
_WRITE_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True)
class OpsEvent:
    """What happened and to what. Ids only: never exception text, arguments or content."""

    code: OpsCode
    tenant_id: uuid.UUID | None
    reason: str | None = None
    """A closed-vocabulary word: a termination reason, exception class name, HTTP status,
    skip reason or category. Anything else is stored as `other`."""
    area: OpsArea | None = None
    """Only to move a `turn.failed` in a direct message to `dm`."""
    severity: OpsSeverity | None = None
    """Only to soften a code whose reasons differ in weight (`wake.failed`); the reason is in
    the fingerprint, so one issue keeps one severity."""
    agent_id: str | None = None
    routine_id: uuid.UUID | None = None
    watch_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    platform: str | None = None
    channel_id: str | None = None
    thread_id: str | None = None
    session_id: str | None = None
    subject_key: str | None = None
    """The bridge server, connector key or wake kind."""
    note: str | None = None
    """Only `routine.goal_unmet`, written by the routine's own agent."""
    workflow_id: uuid.UUID | None = None
    """A workflow's, with its run; `routine_id` is then None."""
    workflow_run_id: uuid.UUID | None = None


@dataclass
class _Window:
    """The repeats of one issue waiting to be written as one row."""

    event: OpsEvent
    record: OpsEventRecord
    count: int = 1
    last_at: datetime | None = None
    """The newest repeat's time, which the row is stamped with, so a repeat after an
    admin resolved the issue regresses it."""
    loop: asyncio.AbstractEventLoop = field(default_factory=asyncio.get_running_loop)
    flush_now: asyncio.Event = field(default_factory=asyncio.Event)


_OPEN: dict[tuple[uuid.UUID, str], _Window] = {}
_PENDING: set[asyncio.Task[None]] = set()


def record_of(event: OpsEvent, *, at: datetime) -> OpsEventRecord:
    """The row an event becomes: its catalog area and severity, cleaned reason and fingerprint."""
    spec = CATALOG[event.code]
    area = event.area if event.code == "turn.failed" and event.area else spec.area
    reason = clean_reason(event.reason)
    subject = {
        "agent": event.agent_id,
        "routine": str(event.routine_id or event.workflow_id or "") or None,
        "watch": str(event.watch_id) if event.watch_id else None,
        "platform": event.platform,
    }.get(spec.subject, event.subject_key)
    return OpsEventRecord(
        occurred_at=at,
        count=1,
        code=event.code,
        area=area,
        severity=event.severity or spec.severity,
        reason=reason,
        fingerprint=fingerprint_of(
            event.code, area, subject, reason, str(event.account_id) if event.account_id else None
        ),
        agent_id=event.agent_id,
        routine_id=event.routine_id,
        watch_id=event.watch_id,
        account_id=event.account_id,
        platform=event.platform,
        channel_id=event.channel_id,
        thread_id=event.thread_id,
        session_id=event.session_id,
        subject_key=event.subject_key,
        release=current_release(),
        note=clean_note(event.note) if event.code == "routine.goal_unmet" else None,
        workflow_id=event.workflow_id,
        workflow_run_id=event.workflow_run_id,
    )


def _log(event: OpsEvent, record: OpsEventRecord) -> None:
    emit = log.error if record.severity == "error" else log.warning
    emit(
        event.code,
        tenant_id=str(event.tenant_id) if event.tenant_id else None,
        area=record.area,
        reason=record.reason,
        agent_id=event.agent_id,
        routine_id=str(event.routine_id) if event.routine_id else None,
        watch_id=str(event.watch_id) if event.watch_id else None,
        account_id=str(event.account_id) if event.account_id else None,
        platform=event.platform,
        subject_key=event.subject_key,
        release=record.release,
    )


async def _write(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, window: _Window
) -> None:
    row = replace(
        window.record, count=window.count, occurred_at=window.last_at or window.record.occurred_at
    )
    try:
        # Always own a connection, even for a caller bound to a checked-out one.
        bind = sm.kw.get("bind")
        if isinstance(bind, AsyncConnection):
            sm = async_sessionmaker(bind.engine, expire_on_commit=False)
        async with asyncio.timeout(_WRITE_TIMEOUT_S):
            async with sm() as session, session.begin():
                await record_ops_events(session, tenant_id=tenant_id, events=[row])
    except Exception as exc:
        # Boundary: reporting must never break the failure path that called it.
        # Never log DB exception strings: SQL parameters can contain identifiers.
        log.warning("ops.event_unrecorded", code=window.event.code, error_class=type(exc).__name__)


async def _flush(
    sm: async_sessionmaker[AsyncSession], key: tuple[uuid.UUID, str], window: _Window
) -> None:
    try:
        async with asyncio.timeout(COALESCE_S):
            await window.flush_now.wait()
    except TimeoutError:
        pass
    # Close the window before writing, so an event during the write opens the next one.
    _OPEN.pop(key, None)
    await _write(sm, key[0], window)


def report_ops_event(
    sm: async_sessionmaker[AsyncSession], event: OpsEvent, *, logged: bool = True
) -> None:
    """Log the event (unless `logged` is False, for a caller that already logs it under
    its own name) and queue its row. Never raises."""
    try:
        record = record_of(event, at=datetime.now(UTC))
        if logged:
            _log(event, record)
        if event.tenant_id is None:
            return
        key = (event.tenant_id, record.fingerprint)
        window = _OPEN.get(key)
        # A window from another event loop (a finished test) has no writer left.
        if window is not None and window.loop is asyncio.get_running_loop():
            window.count += 1
            window.last_at = record.occurred_at
            return
        if len(_PENDING) >= _MAX_PENDING:
            log.warning("ops.event_queue_full", code=event.code)
            return
        window = _Window(event, record)
        _OPEN[key] = window
        task = asyncio.create_task(_flush(sm, key, window), name="ops.event_write")
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
    except Exception as exc:
        # Boundary: reporting must never break the failure path that called it.
        log.warning("ops.event_unrecorded", code=event.code, error_class=type(exc).__name__)


async def drain_ops_events() -> None:
    """Runtime shutdown/test barrier: write every open window now and wait for the writes."""
    loop = asyncio.get_running_loop()
    tasks = [task for task in _PENDING if task.get_loop() is loop]
    for window in list(_OPEN.values()):
        window.flush_now.set()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
