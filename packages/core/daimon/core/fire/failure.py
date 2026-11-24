"""A fire that failed or did not run: its log line, ops event, record and owner notice."""

from __future__ import annotations

import asyncio

import structlog
from daimon.core.fire.store import FireStore
from daimon.core.fire.subject import Subject
from daimon.core.ops_events import OpsEvent, report_ops_event
from daimon.core.turn.termination import TerminationReason
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# Fires that did not run on purpose: logged as `routine.fire_skipped` (info), never as a failure.
BENIGN_FIRE_REASONS = frozenset(
    {"routines_turned_off", "lead_intake_turned_off", "scheduler_shutdown"}
)


async def record_fire_error(
    sm: async_sessionmaker[AsyncSession],
    subject: Subject,
    store: FireStore,
    msg: str,
    reason: TerminationReason | None,
    *,
    error_class: str | None = None,
) -> None:
    """Record a failure on a FRESH session (never reuse fire's session), then tell the owner.

    The log line carries only a fixed reason and the exception class, never `msg`,
    which may hold exception text."""
    fixed = reason.value if reason is not None else msg
    log_fire, event = (
        (log.info, "routine.fire_skipped")
        if fixed in BENIGN_FIRE_REASONS
        else (log.warning, "routine.fire_failed")
    )
    log_fire(
        event,
        tenant_id=str(subject.tenant_id),
        routine_id=str(subject.id),
        agent_id=subject.agent_id,
        reason=fixed,
        error_class=error_class,
    )
    if event == "routine.fire_failed":
        report_ops_event(
            sm,
            OpsEvent(
                event,
                subject.tenant_id,
                reason=error_class or fixed,
                routine_id=subject.id,
                agent_id=subject.agent_id,
            ),
            logged=False,
        )
    try:
        async with asyncio.timeout(1), sm() as s, s.begin():
            told = await store.fail(s, error=msg, reason=reason)
    except Exception:
        log.exception("record_result(error) failed", routine_id=str(subject.id))
        return
    store.tell_owner(reason, channel_told=told)
