"""A fire that failed or did not run: its log line, ops event, record and owner notice.

`report_failure` is the log line and ops event of every unattended run, a routine's fire
or a live workflow run, keyed by its routine or its workflow."""

from __future__ import annotations

import asyncio
import uuid

import structlog
from daimon.core.fire.store import FireGuardStore
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
    store: FireGuardStore,
    msg: str,
    reason: TerminationReason | None,
    *,
    error_class: str | None = None,
) -> None:
    """Record a failure on a FRESH session (never reuse fire's session), then tell the owner.

    The log line carries only a fixed reason and the exception class, never `msg`,
    which may hold exception text."""
    fixed = reason.value if reason is not None else msg
    report_failure(
        sm,
        subject.tenant_id,
        fixed,
        error_class=error_class,
        agent_id=subject.agent_id,
        routine_id=subject.id,
    )
    try:
        async with asyncio.timeout(1), sm() as s, s.begin():
            told = await store.fail(s, error=msg, reason=reason)
    except Exception:
        log.exception("record_result(error) failed", routine_id=str(subject.id))
        return
    store.tell_owner(reason, channel_told=told)


def report_failure(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    reason: str,
    *,
    error_class: str | None = None,
    agent_id: str | None = None,
    routine_id: uuid.UUID | None = None,
    workflow_id: uuid.UUID | None = None,
    workflow_run_id: uuid.UUID | None = None,
) -> None:
    """Log an unattended run that failed or did not run and, unless that was on purpose,
    raise its ops event. `reason` is a fixed word, never exception text."""
    benign = reason in BENIGN_FIRE_REASONS
    event = "routine.fire_skipped" if benign else "routine.fire_failed"
    ids = {"routine_id": routine_id, "workflow_id": workflow_id, "workflow_run_id": workflow_run_id}
    (log.info if benign else log.warning)(
        event,
        tenant_id=str(tenant_id),
        **{k: str(v) for k, v in ids.items() if v is not None},
        agent_id=agent_id,
        reason=reason,
        error_class=error_class,
    )
    if not benign:
        report_ops_event(
            sm,
            OpsEvent(
                "routine.fire_failed",
                tenant_id,
                reason=error_class or reason,
                routine_id=routine_id,
                agent_id=agent_id,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
            ),
            logged=False,
        )
