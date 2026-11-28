"""How a run's end is recorded, and the failure boundary for one run's work in a loop.

`runs` ends runs through the engine's transitions. `contained` ends one the engine cannot
go on with: a stored spec this server no longer reads (a rollback) or an engine bug. Such
a run would raise on every tick and hold up the work queued behind it in a loop every
workspace shares, so it fails with a plain reason, needing no spec.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Final

import anthropic
import httpx
import structlog
from daimon.core.fire.failure import report_failure
from daimon.core.stores import security_audit, workflow_runs, workflow_waits
from daimon.core.stores.workflow_run_events import NewEvent, append_events
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.workflows import asks
from pydantic import ValidationError
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

ENGINE: Final = "engine"
#: What an ask still open when its run ends says, by how the run ended.
ASK_CLOSED: Final = {
    "cancelled": "Cancelled: the run was stopped.",
    "failed": "Closed: the run failed.",
    "timed_out": "Expired: the run ran out of time.",
}
ASK_ENDED: Final = "Closed: the run ended."
#: Errors the next tick may not meet again: the item is left for it, its run untouched.
#: A stored spec this version cannot read is one: specs are validated at publish, so it was
#: written by another version (a rolling deploy or a rollback), and a scheduler of that
#: version will read it.
TRANSIENT: Final = (SQLAlchemyError, anthropic.APIError, httpx.HTTPError, ValidationError)
BROKEN: Final = "engine_error"


def run_channel(tenant_id: uuid.UUID) -> str:
    """The NOTIFY channel the run page's stream listens on."""
    return f"workflow_run_{tenant_id.hex}"


def report_after_commit(session: AsyncSession, run: RunRow, reason: str) -> None:
    """A failed live run's log line and ops event, as a routine fire's (`report_failure`),
    once the run's end is committed; its owner's notice is `queue_failure_notice`."""
    bind = session.bind

    def report(_: object) -> None:
        report_failure(
            async_sessionmaker(bind=bind),
            run.tenant_id,
            reason,
            workflow_id=run.workflow_id,
            workflow_run_id=run.id,
        )

    sa_event.listen(session.sync_session, "after_commit", report, once=True)


async def audit_end(
    session: AsyncSession, run: RunRow, status: str, head: str | None, now: datetime
) -> None:
    """A run's end in the security audit, with its ledger head."""
    seq = await workflow_runs.get_run(session, run.id)
    await security_audit.append_event(
        session,
        tenant_id=run.tenant_id,
        account_id=run.run_as_account_id,
        agent_id=None,
        platform=None,
        platform_user_id=None,
        tool_name="workflow_run",
        operation=status,
        outcome="allowed",
        reason=f"run {run.id} seq {seq.last_seq if seq else 0} head {head}",
        occurred_at=now,
    )


async def fail_broken(
    sm: async_sessionmaker[AsyncSession], *, run_id: uuid.UUID, error: Exception, now: datetime
) -> bool:
    """Fail an active run without reading its spec: its steps are cancelled and its asks
    closed. False when it already ended."""
    message = f"The run stopped on an error Daimon could not get past ({type(error).__name__})."
    async with sm.begin() as session:
        run = await workflow_runs.get_run(session, run_id, lock=True)
        if run is None or run.status not in workflow_runs.ACTIVE_RUN_STATES:
            return False
        await workflow_runs.cancel_active_steps(session, run_id=run_id, now=now)
        await workflow_runs.update_run(
            session,
            run_id,
            status="failed",
            waiting_reason=None,
            outcome=None,
            failure_reason=BROKEN,
            failure_message=message,
            failure_step=None,
            ended_at=now,
        )
        closed = await workflow_runs.close_waits(
            session, status="cancelled", now=now, run_id=run_id
        )
        for wait in (await workflow_waits.waits_by_id(session, closed)).values():
            await asks.queue_updates(session, wait, ASK_CLOSED["failed"])
        data = {"reason": BROKEN, "message": message}
        head = await append_events(
            session,
            run_id=run_id,
            events=[NewEvent("run.failed", None, data)],
            actor=ENGINE,
            at=now,
        )
        await audit_end(session, run, "failed", head, now)
        await session.execute(select(func.pg_notify(run_channel(run.tenant_id), str(run_id))))
        if run.mode == "live":
            report_after_commit(session, run, BROKEN)
    return True


@contextlib.asynccontextmanager
async def contained(
    sm: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    run_id: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
) -> AsyncIterator[None]:
    """One run's work in a shared loop: a transient error leaves it for the next tick, any
    other fails the run (`fail_broken`) and the loop goes on to the next item."""
    try:
        yield
    except TRANSIENT as err:
        log.warning("workflows.item.deferred", run_id=str(run_id), error=type(err).__name__)
    except Exception as err:  # named boundary: one run's error never stops the shared loops
        log.exception("workflows.item.failed", run_id=str(run_id), step_id=str(step_id))
        if run_id is None and step_id is not None:
            async with sm() as session:
                run_id = await workflow_runs.run_of_step(session, step_id)
        if run_id is not None:
            await fail_broken(sm, run_id=run_id, error=err, now=now)
