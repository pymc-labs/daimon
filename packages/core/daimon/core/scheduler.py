"""Routine dispatch and task ownership. Process lifecycle lives in adapters/scheduler/."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from daimon.core.ids import generate_request_id
from daimon.core.observability import capture_exception_with_scope
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    advance_stale,
    claim_due_fireable,
    record_result,
    skip_slots_during_fire,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class CapsCheck(Protocol):
    async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool: ...


FireFn = Callable[[RoutineRow], Awaitable[None]]


class RoutineDispatcher:
    """Own bounded in-flight tasks across ticks; the scheduler owns shutdown."""

    def __init__(
        self, max_concurrent_fires: int, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        if max_concurrent_fires < 1:
            raise ValueError("max_concurrent_fires must be positive")
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_concurrent_fires = max_concurrent_fires
        self.semaphore = asyncio.Semaphore(max_concurrent_fires)
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._versions: dict[uuid.UUID, datetime] = {}

    @property
    def in_flight_ids(self) -> frozenset[uuid.UUID]:
        return frozenset(key for key, task in self._tasks.items() if not task.done())

    @property
    def in_flight_versions(self) -> dict[uuid.UUID, datetime]:
        return {key: self._versions[key] for key in self.in_flight_ids}

    @property
    def available(self) -> int:
        return max(0, self.max_concurrent_fires - len(self.in_flight_ids))

    def start(
        self, routine_id: uuid.UUID, work: Coroutine[object, object, None], *, updated_at: datetime
    ) -> None:
        task = asyncio.create_task(work)
        self._tasks[routine_id] = task
        self._versions[routine_id] = updated_at

        def finished(done: asyncio.Task[None]) -> None:
            if self._tasks.get(routine_id) is done:
                del self._tasks[routine_id]
                del self._versions[routine_id]

        task.add_done_callback(finished)

    async def drain(self) -> None:
        """Wait for already-dispatched work (one-shot execution)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks.values()))

    async def close(self) -> None:
        """Cancel and join fires before releasing database/client resources."""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _record_fire_error(
    sm: async_sessionmaker[AsyncSession], routine_id: uuid.UUID, msg: str
) -> None:
    """Record a fire failure on a FRESH session (never reuse fire's session)."""
    try:
        async with sm() as s, s.begin():
            await record_result(s, routine_id, tail=None, error=msg)
    except Exception:
        log.exception("record_result(error) failed", routine_id=str(routine_id))


async def run_one_tick(
    *,
    now: datetime,
    sm: async_sessionmaker[AsyncSession],
    caps: CapsCheck,
    fire: FireFn,
    max_age: timedelta,
    max_concurrent_fires: int,
    dispatch_timeout_s: float,
    dispatcher: RoutineDispatcher | None = None,
    wait_for_completion: bool = False,
) -> RoutineDispatcher:
    """Claim and dispatch a tick without awaiting routine completion.

    Reuse the returned dispatcher across ticks and close it on shutdown.
    Set wait_for_completion=True for a one-shot batch. Active routines are
    excluded and their intervening slots are recorded as skipped.
    Capacity-limited claims keep the pending queue in PostgreSQL.
    """
    one_shot = wait_for_completion
    loop = asyncio.get_running_loop()
    tick_started = loop.time()
    dispatcher = dispatcher or RoutineDispatcher(
        max_concurrent_fires, clock=lambda: now + timedelta(seconds=loop.time() - tick_started)
    )
    active_versions = dispatcher.in_flight_versions
    active_ids = frozenset(active_versions)
    async with sm() as session, session.begin():
        try:
            await advance_stale(
                session, now=now, max_age=max_age, in_flight_versions=active_versions
            )
        except Exception:
            log.exception("advance_stale failed")
        try:
            rows = await claim_due_fireable(
                session,
                now=now,
                max_age=max_age,
                limit=20 if one_shot else min(20, dispatcher.available),
                exclude_ids=active_ids,
            )
        except Exception:
            log.exception("claim_due_fireable failed")
            return dispatcher

    # Sequential cap check (cheaper
    # than checking inside the semaphore boundary; keeps the existing cap path).
    fireable: list[RoutineRow] = []
    for row in rows:
        if row.created_by_user_id is None:
            # No user to bill against — treat as uncapped (exemption applies
            # symmetrically to routines without an attributable owner).
            over = False
        else:
            try:
                over = await caps.is_over_cap(row.tenant_id, row.created_by_user_id)
            except Exception:
                log.exception("caps.is_over_cap failed", routine_id=str(row.id))
                continue
        if over:
            try:
                async with sm() as s, s.begin():
                    await record_result(s, row.id, tail=None, error="cap_exceeded")
            except Exception:
                log.exception("record_result(cap-block) failed", routine_id=str(row.id))
            continue
        fireable.append(row)

    # A persistent semaphore bounds fires across every tick.
    sem = dispatcher.semaphore
    clock = dispatcher.clock

    async def _fire_guarded(row: RoutineRow) -> None:
        # Per-fire correlation context: every log line emitted inside this
        # fire — claim/advance/record-result and the turn body — carries a fresh
        # rid and the row's tenant_id. Unbind in finally so the context is clean
        # even on the error paths.
        rid = generate_request_id()
        structlog.contextvars.bind_contextvars(rid=rid, tenant_id=str(row.tenant_id))
        try:
            async with sem:
                try:
                    await asyncio.wait_for(fire(row), timeout=dispatch_timeout_s)
                except asyncio.CancelledError:
                    await _record_fire_error(sm, row.id, "scheduler_shutdown")
                    raise
                except TimeoutError as err:
                    # Capture to Sentry, then keep the existing swallow —
                    # sibling tasks continue and record_result still runs.
                    capture_exception_with_scope(err)
                    await _record_fire_error(sm, row.id, f"timeout: exceeded {dispatch_timeout_s}s")
                except Exception as err:
                    capture_exception_with_scope(err)
                    await _record_fire_error(sm, row.id, f"{type(err).__name__}: {err}"[:500])
        finally:
            try:
                async with sm() as session, session.begin():
                    await skip_slots_during_fire(
                        session,
                        routine_id=row.id,
                        finished_at=clock(),
                        expected_updated_at=row.updated_at,
                    )
            except Exception:
                log.exception("scheduler.completion_skip_failed", routine_id=str(row.id))
            finally:
                structlog.contextvars.unbind_contextvars("rid", "tenant_id")

    # Pitfall 2: spawn each fire as its own task so asyncio.create_task copies the
    # current contextvars context at creation — concurrent fires then bind into
    # ISOLATED contexts and never cross-contaminate rids. Bare coroutines in gather
    # would share one context (the semaphore alone does NOT fix this).
    for row in fireable:
        dispatcher.start(row.id, _fire_guarded(row), updated_at=row.updated_at)
    if one_shot:
        await dispatcher.drain()

    log.info(
        "scheduler.tick",
        claimed=len(rows),
        fired=len(fireable),
    )
    return dispatcher
