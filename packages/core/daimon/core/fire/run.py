"""The guard around one fire: feature gates, execution grant, dispatch bound, failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from daimon.core.features.snapshot import FeaturePlace, place_feature_or_none
from daimon.core.fire.failure import record_fire_error
from daimon.core.fire.store import FireStatus, FireStore
from daimon.core.fire.subject import Subject
from daimon.core.observability import capture_exception_with_scope
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.termination import TerminationReason, termination_reason
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Dispatch:
    """What the dispatcher lends one fire: its semaphore, shared with every fire, and bound."""

    sm: async_sessionmaker[AsyncSession]
    semaphore: asyncio.Semaphore
    timeout_s: float
    capture: Callable[[BaseException], None] = capture_exception_with_scope


async def fire(
    subject: Subject,
    store: FireStore,
    turn: Callable[[], Awaitable[None]],
    dispatch: Dispatch,
    *,
    routines_gate: bool,
    intake_bound: bool,
    settle: Callable[[FireStatus], Awaitable[None]] | None = None,
) -> FireStatus:
    """Run `turn` under the feature gates, the execution grant and the dispatch bound.

    `routines_gate` reads the `routines` feature first; `intake_bound` (bound as lead
    intake) reads `lead_intake` too. A failure is recorded through `store`; `settle` runs
    last, with the status, even when the fire is cancelled.
    """
    sm = dispatch.sm
    timeout_s = dispatch.timeout_s
    loop = asyncio.get_running_loop()
    observation = TurnObservation(
        sm, subject.tenant_id, "scheduler", origin="routine", agent_id=subject.agent_id
    )
    unreadable = False
    started = loop.time()
    outcome: FireStatus = "ok"
    skip_reason: str | None = None
    try:
        async with dispatch.semaphore:
            try:
                place = FeaturePlace(
                    subject.tenant_id, "", subject.channel_id, agent_id=subject.agent_id
                )
                if routines_gate:
                    routines = await place_feature_or_none(sm, None, place, "routines")
                    if routines is None or not routines.on:
                        # Off here, or unreadable: the schedule does not run this tick.
                        # Unreadable is not Off: the owner sees why, and the slot is given
                        # back so the next tick tries again, as a watch's poll does.
                        unreadable = routines is None
                        reason = "feature_read_failed" if unreadable else "routines_turned_off"
                        outcome, skip_reason = "skipped", reason
                        await record_fire_error(sm, subject, store, reason, None)
                        return "unreadable" if unreadable else outcome
                if intake_bound:
                    intake = await place_feature_or_none(sm, None, place, "lead_intake")
                    if intake is None or not intake.on:
                        unreadable = intake is None
                        reason = "feature_read_failed" if unreadable else "lead_intake_turned_off"
                        outcome, skip_reason = "skipped", reason
                        await record_fire_error(sm, subject, store, reason, None)
                        return "unreadable" if unreadable else outcome
                with observation.activate():
                    async with store.execution(timeout_s):
                        await asyncio.wait_for(turn(), timeout=timeout_s)
            except TimeoutError as err:
                outcome = "failed"
                observation.finish(error=err, reason=TerminationReason.CEILING)
                # Capture to Sentry, then keep the existing swallow —
                # sibling tasks continue and the result is still recorded.
                dispatch.capture(err)
                timeout = f"timeout: exceeded {timeout_s}s"
                await record_fire_error(sm, subject, store, timeout, TerminationReason.CEILING)
            except Exception as err:
                outcome = "failed"
                reason = termination_reason(err)
                observation.finish(error=err, reason=reason)
                dispatch.capture(err)
                await record_fire_error(
                    sm,
                    subject,
                    store,
                    "execution_failed"
                    if subject.destination_kind == "dm"
                    else f"{type(err).__name__}: {err}"[:500],
                    reason,
                    error_class=type(err).__name__,
                )
    except asyncio.CancelledError as err:
        outcome = "failed"
        observation.finish(error=err)
        # Includes cancellation while waiting for a dispatch slot.
        await record_fire_error(sm, subject, store, "scheduler_shutdown", None)
        raise
    finally:
        observation.finish(reason=TerminationReason.UNKNOWN, fallback=True)
        log.info(
            "routine.finished",
            tenant_id=str(subject.tenant_id),
            routine_id=str(subject.id),
            agent_id=subject.agent_id,
            outcome=outcome,
            skip_reason=skip_reason,
            duration_ms=int((loop.time() - started) * 1000),
        )
        if settle is not None:
            await settle("unreadable" if unreadable else outcome)
    return outcome
