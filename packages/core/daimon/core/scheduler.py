"""Routine dispatch and task ownership. Process lifecycle lives in adapters/scheduler/."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta

import structlog
from daimon.core.features.snapshot import FeaturePlace, place_feature_or_none
from daimon.core.ids import generate_request_id
from daimon.core.observability import capture_exception_with_scope
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.routine_delivery import record_routine_failure
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.lead_intake import configured_routines, routine_execution
from daimon.core.stores.routines import (
    advance_stale,
    claim_due_fireable,
    record_result,
    release_claimed_slot,
    skip_slots_during_fire,
)
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.termination import TerminationReason, termination_reason
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

log = structlog.get_logger(__name__)


FireFn = Callable[[RoutineRow], Awaitable[None]]
#: Tells a routine's owner that a run failed: the routine, why (a `TerminationReason` or a
#: refusal's fixed word), and whether its channel was told.
OwnerNotifier = Callable[[RoutineRow, str, bool], Awaitable[None]]


_NOTICES: set[asyncio.Task[None]] = set()


def tell_owner(
    notify: OwnerNotifier | None,
    row: RoutineRow,
    reason: str | None,
    *,
    channel_told: bool,
) -> None:
    """Tell the owner of a failed run, once per failing streak, as the channel notice is.

    `row` is the routine as claimed, so a clear `last_error` means this failure starts the
    streak. `reason` None (a deploy cutting the run short) tells nobody. The notice runs as
    its own task, in a fresh context: never inside the fire's slot, timeout, grant or
    observation, so a slow notice stalls no routine. `drain_owner_notices` ends them.
    """
    if notify is None or reason is None or row.last_error is not None:
        return
    task = asyncio.create_task(
        _notify(notify, row, reason, channel_told),
        name="routine.owner_notice",
        context=contextvars.Context(),
    )
    _NOTICES.add(task)
    task.add_done_callback(_NOTICES.discard)


async def _notify(notify: OwnerNotifier, row: RoutineRow, reason: str, channel_told: bool) -> None:
    try:
        await notify(row, reason, channel_told)
    except Exception as exc:
        # Boundary: the failure is already recorded; telling the owner must not undo that.
        log.warning(
            "routine.owner_notice_unhandled", routine_id=str(row.id), error_class=type(exc).__name__
        )


async def drain_owner_notices(timeout_s: float) -> None:
    """Shutdown/test barrier: let running owner notices finish, then cancel what is left."""
    loop = asyncio.get_running_loop()
    if tasks := [task for task in _NOTICES if task.get_loop() is loop]:
        _, late = await asyncio.wait(tasks, timeout=timeout_s)
        for task in late:
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)


class RoutineDispatcher:
    """Own a bounded batch of running and queued fires across ticks."""

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
        # Preserve the predecessor's 20-row claim batch even when some fires
        # must wait for the semaphore. Already-eligible slots must not age out
        # merely because the concurrency limit is smaller than the batch.
        return max(0, max(20, self.max_concurrent_fires) - len(self.in_flight_ids))

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


# Fires that did not run on purpose: logged as `routine.fire_skipped` (info), never as a failure.
_BENIGN_FIRE_REASONS = frozenset(
    {"routines_turned_off", "lead_intake_turned_off", "scheduler_shutdown"}
)


async def _record_fire_error(
    sm: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    msg: str,
    reason: TerminationReason | None,
    *,
    error_class: str | None = None,
    notify_owner: OwnerNotifier | None = None,
) -> None:
    """Record a failure on a FRESH session (never reuse fire's session), then tell the owner.

    The log line carries only a fixed reason and the exception class, never `msg`,
    which may hold exception text."""
    fixed = reason.value if reason is not None else msg
    log_fire, event = (
        (log.info, "routine.fire_skipped")
        if fixed in _BENIGN_FIRE_REASONS
        else (log.warning, "routine.fire_failed")
    )
    log_fire(
        event,
        tenant_id=str(row.tenant_id),
        routine_id=str(row.id),
        agent_id=row.agent_id,
        reason=fixed,
        error_class=error_class,
    )
    if event == "routine.fire_failed":
        report_ops_event(
            sm,
            OpsEvent(
                event,
                row.tenant_id,
                reason=error_class or fixed,
                routine_id=row.id,
                agent_id=row.agent_id,
            ),
            logged=False,
        )
    try:
        async with asyncio.timeout(1), sm() as s, s.begin():
            told = await record_routine_failure(s, row, error=msg, reason=reason)
    except Exception:
        log.exception("record_result(error) failed", routine_id=str(row.id))
        return
    tell_owner(notify_owner, row, reason, channel_told=told)


async def record_refusal(
    s: AsyncSession,
    sm: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    reason: str,
    *,
    error: str | None = None,
    fault: bool = False,
    notify_owner: OwnerNotifier | None,
    expected_destination: tuple[str | None, str | None] | None = None,
    expected_private: RoutineRow | None = None,
    expected_row: RoutineRow | None = None,
) -> None:
    """Record a fire refused before it ran, on `s`, as an issue; tell the owner once `s` commits.

    `reason` is a fixed word; `error` the text the routine panel shows, else `reason`. A
    `fault` (Daimon's, not the owner's) is logged as `routine.fire_failed`, which alerting
    counts; a refusal the owner can fix logs `routine.fire_refused` at info. Both record
    a `routine.fire_failed` issue, a refusal at warning. `notify_owner` None tells nobody.
    """
    await record_result(
        s,
        row.id,
        tail=None,
        error=error or reason,
        expected_destination=expected_destination,
        expected_private=expected_private,
        expected_row=expected_row,
    )
    (log.warning if fault else log.info)(
        "routine.fire_failed" if fault else "routine.fire_refused",
        tenant_id=str(row.tenant_id),
        routine_id=str(row.id),
        agent_id=row.agent_id,
        reason=reason,
    )
    report_ops_event(
        sm,
        OpsEvent(
            "routine.fire_failed",
            row.tenant_id,
            reason=reason,
            severity=None if fault else "warning",
            routine_id=row.id,
            agent_id=row.agent_id,
        ),
        logged=False,
    )

    # After the commit, so a rolled-back refusal tells nobody and its retry tells once.
    def told_after_commit(_: Session) -> None:
        tell_owner(notify_owner, row, reason, channel_told=False)

    event.listen(s.sync_session, "after_commit", told_after_commit, once=True)


async def _release_slot(sm: async_sessionmaker[AsyncSession], row: RoutineRow) -> None:
    """Give the claimed slot back, so the next tick fires it (within the catch-up window)."""
    try:
        async with asyncio.timeout(1), sm() as s, s.begin():
            await release_claimed_slot(s, row)
    except Exception:
        log.exception("release_claimed_slot failed", routine_id=str(row.id))


async def run_one_tick(
    *,
    now: datetime,
    sm: async_sessionmaker[AsyncSession],
    fire: FireFn,
    max_age: timedelta,
    max_concurrent_fires: int,
    dispatch_timeout_s: float,
    dispatcher: RoutineDispatcher | None = None,
    wait_for_completion: bool = False,
    notify_owner: OwnerNotifier | None = None,
) -> RoutineDispatcher:
    """Claim and dispatch a tick without awaiting routine completion.

    Reuse the returned dispatcher across ticks and close it on shutdown.
    Set wait_for_completion=True for a one-shot batch. Active routines are
    excluded and their intervening slots are recorded as skipped.
    Retain a bounded claimed batch, including work waiting for the semaphore.
    `notify_owner` tells the owner of a run that failed (`tell_owner`).
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
                limit=min(20, dispatcher.available),
                exclude_ids=active_ids,
            )
        except Exception:
            log.exception("claim_due_fireable failed")
            return dispatcher

    try:
        async with sm() as intake_session:
            intake_bindings = await configured_routines(
                intake_session, {row.tenant_id for row in rows}
            )
    except Exception:
        log.exception("configured_routines failed")
        for row in rows:
            await _record_fire_error(sm, row, "routine_configuration_check_failed", None)
        return dispatcher

    fireable: list[RoutineRow] = list(rows)

    # A persistent semaphore bounds fires across every tick.
    sem = dispatcher.semaphore
    clock = dispatcher.clock

    async def _fire_guarded(row: RoutineRow) -> None:
        # Per-fire correlation context: every log line emitted inside this
        # fire — claim/advance/record-result and the turn body — carries a fresh
        # rid and the row's tenant_id. Unbind in finally so the context is clean
        # even on the error paths.
        observation = TurnObservation(
            sm, row.tenant_id, "scheduler", origin="routine", agent_id=row.agent_id
        )
        rid = generate_request_id()
        structlog.contextvars.bind_contextvars(rid=rid, tenant_id=str(row.tenant_id))
        unreadable = False
        started = loop.time()
        outcome = "ok"
        skip_reason: str | None = None
        try:
            async with sem:
                try:
                    place = FeaturePlace(row.tenant_id, "", row.channel_id, agent_id=row.agent_id)
                    routines = await place_feature_or_none(sm, None, place, "routines")
                    if routines is None or not routines.on:
                        # Off here, or unreadable: the schedule does not run this tick.
                        # Unreadable is not Off: the owner sees why, and the slot is given
                        # back below so the next tick tries again, as a watch's poll does.
                        unreadable = routines is None
                        reason = "feature_read_failed" if unreadable else "routines_turned_off"
                        outcome, skip_reason = "skipped", reason
                        await _record_fire_error(sm, row, reason, None)
                        return
                    if (row.tenant_id, row.id) in intake_bindings:
                        intake = await place_feature_or_none(sm, None, place, "lead_intake")
                        if intake is None or not intake.on:
                            unreadable = intake is None
                            reason = (
                                "feature_read_failed" if unreadable else "lead_intake_turned_off"
                            )
                            outcome, skip_reason = "skipped", reason
                            await _record_fire_error(sm, row, reason, None)
                            return
                    with observation.activate():
                        async with routine_execution(
                            sm,
                            row.tenant_id,
                            row.id,
                            lifetime_s=dispatch_timeout_s,
                        ):
                            await asyncio.wait_for(fire(row), timeout=dispatch_timeout_s)
                except TimeoutError as err:
                    outcome = "failed"
                    observation.finish(error=err, reason=TerminationReason.CEILING)
                    # Capture to Sentry, then keep the existing swallow —
                    # sibling tasks continue and record_result still runs.
                    capture_exception_with_scope(err)
                    timeout = f"timeout: exceeded {dispatch_timeout_s}s"
                    await _record_fire_error(
                        sm, row, timeout, TerminationReason.CEILING, notify_owner=notify_owner
                    )
                except Exception as err:
                    outcome = "failed"
                    reason = termination_reason(err)
                    observation.finish(error=err, reason=reason)
                    capture_exception_with_scope(err)
                    await _record_fire_error(
                        sm,
                        row,
                        "execution_failed"
                        if row.destination_kind == "dm"
                        else f"{type(err).__name__}: {err}"[:500],
                        reason,
                        error_class=type(err).__name__,
                        notify_owner=notify_owner,
                    )
        except asyncio.CancelledError as err:
            outcome = "failed"
            observation.finish(error=err)
            # Includes cancellation while waiting for a dispatch slot.
            await _record_fire_error(sm, row, "scheduler_shutdown", None)
            raise
        finally:
            observation.finish(reason=TerminationReason.UNKNOWN, fallback=True)
            log.info(
                "routine.finished",
                tenant_id=str(row.tenant_id),
                routine_id=str(row.id),
                agent_id=row.agent_id,
                outcome=outcome,
                skip_reason=skip_reason,
                duration_ms=int((loop.time() - started) * 1000),
            )
            try:
                async with asyncio.timeout(1), sm() as session, session.begin():
                    await skip_slots_during_fire(
                        session,
                        routine_id=row.id,
                        finished_at=clock(),
                        expected_updated_at=row.updated_at,
                    )
            except Exception:
                log.exception("scheduler.completion_skip_failed", routine_id=str(row.id))
            finally:
                if unreadable:
                    # After the skip, which would otherwise pass over the restored slot.
                    await _release_slot(sm, row)
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
