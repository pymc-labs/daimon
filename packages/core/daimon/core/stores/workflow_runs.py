"""Store for workflow runs, their steps, attempts and waits, as the engine loops use them.

Lock order is always run row, then step rows, so a settle (which locks the run to append
to its ledger) and a claim never wait on each other in a cycle. A claim takes both with
SKIP LOCKED, bumps the step's `generation` and leases it to one worker; a settle names
the generation it ran under and is refused when the step moved on (cancelled, redriven,
reclaimed) meanwhile.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, TypedDict, Unpack

from daimon.core._models import (
    Workflow,
    WorkflowAttempt,
    WorkflowGrant,
    WorkflowRun,
    WorkflowStep,
    WorkflowTrigger,
    WorkflowTriggerEvent,
    WorkflowWait,
)
from daimon.core.workflows.engine import ErrorClass, StepStatus
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

ACTIVE_RUN_STATES = ("queued", "running", "waiting")
AttemptStatus = Literal["running", "succeeded", "failed", "abandoned"]


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


class RunRow(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    version: int
    number: int
    mode: Literal["live", "test"]
    status: str
    waiting_reason: str | None
    outcome: str | None
    failure_reason: str | None
    failure_message: str | None
    failure_step: str | None
    run_as_account_id: uuid.UUID | None
    triggered_by: dict[str, Any]
    trigger_kind: str
    trigger_key: str
    inputs: dict[str, Any]
    untrusted_inputs: bool
    label: str | None
    parent_run_id: uuid.UUID | None
    forked_from_run_id: uuid.UUID | None
    forked_from_step: str | None
    redrive_count: int
    test_options: dict[str, Any] | None
    budget_usd: Decimal
    cost_usd: Decimal
    deadline_at: datetime | None
    last_seq: int
    head_hash: str | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime


class StepRow(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    node_id: str
    visit: int
    status: StepStatus
    waiting_reason: str | None
    output: Any
    summary: str | None
    route: str | None
    error_class: ErrorClass | None
    error_message: str | None
    via: list[str]
    reused: Literal["fork", "pinned"] | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    generation: int
    cost_usd: Decimal
    ready_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None


class AttemptRow(_Row):
    id: uuid.UUID
    step_id: uuid.UUID
    number: int
    status: AttemptStatus
    error_class: str | None
    send_intent_at: datetime | None
    started_at: datetime
    ended_at: datetime | None


class WaitRow(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    step_id: uuid.UUID
    kind: str
    status: str
    expires_at: datetime | None
    remind_at: datetime | None
    shown: dict[str, Any] | None
    opened_at: datetime


class StepWrite(TypedDict, total=False):
    """A step's new state, keyed by `(node_id, visit)` in `save_steps`."""

    status: str
    waiting_reason: str | None
    output: Any
    summary: str | None
    route: str | None
    error_class: str | None
    error_message: str | None
    via: list[str]
    reused: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    ready_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None


class RunChanges(TypedDict, total=False):
    status: str
    waiting_reason: str | None
    outcome: str | None
    failure_reason: str | None
    failure_message: str | None
    failure_step: str | None
    budget_usd: Decimal
    deadline_at: datetime | None
    redrive_count: int
    started_at: datetime | None
    ended_at: datetime | None


async def create_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    version: int,
    mode: Literal["live", "test"],
    run_as_account_id: uuid.UUID | None,
    triggered_by: dict[str, Any],
    trigger_kind: str,
    trigger_key: str,
    inputs: dict[str, Any],
    budget_usd: Decimal,
    deadline_at: datetime | None,
    untrusted_inputs: bool = False,
    label: str | None = None,
    parent_run_id: uuid.UUID | None = None,
    forked_from_run_id: uuid.UUID | None = None,
    forked_from_step: str | None = None,
    test_options: dict[str, Any] | None = None,
) -> RunRow:
    """A queued run, numbered from the workflow's counter. Only `runs.start_run` calls this."""
    number = await session.scalar(
        update(Workflow)
        .where(Workflow.id == workflow_id, Workflow.tenant_id == tenant_id)
        .values(run_counter=Workflow.run_counter + 1)
        .returning(Workflow.run_counter)
    )
    if number is None:
        raise LookupError(f"No workflow {workflow_id}")
    orm = WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        version=version,
        number=number,
        mode=mode,
        run_as_account_id=run_as_account_id,
        triggered_by=triggered_by,
        trigger_kind=trigger_kind,
        trigger_key=trigger_key,
        inputs=inputs,
        untrusted_inputs=untrusted_inputs,
        label=label,
        parent_run_id=parent_run_id,
        forked_from_run_id=forked_from_run_id,
        forked_from_step=forked_from_step,
        test_options=test_options,
        budget_usd=budget_usd,
        deadline_at=deadline_at,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return RunRow.model_validate(orm)


async def get_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
    lock: bool = False,
    skip_locked: bool = False,
) -> RunRow | None:
    """The run; `lock` holds its row (the ledger's seq lock), `skip_locked` gives up at once."""
    stmt = select(WorkflowRun).where(WorkflowRun.id == run_id)
    if tenant_id is not None:
        stmt = stmt.where(WorkflowRun.tenant_id == tenant_id)
    if lock:
        stmt = stmt.with_for_update(skip_locked=skip_locked)
    orm = (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    return None if orm is None else RunRow.model_validate(orm)


async def update_run(
    session: AsyncSession, run_id: uuid.UUID, **changes: Unpack[RunChanges]
) -> None:
    await session.execute(update(WorkflowRun).where(WorkflowRun.id == run_id).values(**changes))


async def add_cost(
    session: AsyncSession, *, run_id: uuid.UUID, step_id: uuid.UUID, cost_usd: Decimal
) -> None:
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .values(cost_usd=WorkflowRun.cost_usd + cost_usd)
    )
    await session.execute(
        update(WorkflowStep)
        .where(WorkflowStep.id == step_id)
        .values(cost_usd=WorkflowStep.cost_usd + cost_usd)
    )


async def list_steps(session: AsyncSession, run_id: uuid.UUID) -> list[StepRow]:
    rows = await session.scalars(
        select(WorkflowStep)
        .where(WorkflowStep.run_id == run_id)
        .order_by(WorkflowStep.created_at, WorkflowStep.node_id, WorkflowStep.visit)
        .execution_options(populate_existing=True)
    )
    return [StepRow.model_validate(orm) for orm in rows]


async def get_step(session: AsyncSession, step_id: uuid.UUID) -> StepRow | None:
    stmt = select(WorkflowStep).where(WorkflowStep.id == step_id)
    orm = (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    return None if orm is None else StepRow.model_validate(orm)


async def save_steps(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    steps: Mapping[tuple[str, int], StepWrite],
    bump_generation: bool = False,
) -> None:
    """Insert or update steps by `(node_id, visit)`; the caller holds the run lock.

    `bump_generation` makes a running worker's settle stale (cancel, end of run).
    """
    for (node_id, visit), write in steps.items():
        stmt = insert(WorkflowStep).values(
            tenant_id=tenant_id, run_id=run_id, node_id=node_id, visit=visit, **write
        )
        changes: dict[str, Any] = dict(write)
        if bump_generation:
            changes["generation"] = WorkflowStep.generation + 1
        await session.execute(
            stmt.on_conflict_do_update(constraint="uq_workflow_steps_visit", set_=changes)
            if changes
            else stmt.on_conflict_do_nothing()
        )


async def ready_steps(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]]:
    """Due ready steps, oldest first: `(step id, tenant, run-as account)`."""
    rows = await session.execute(
        select(WorkflowStep.id, WorkflowStep.tenant_id, WorkflowRun.run_as_account_id)
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
        .where(
            WorkflowStep.status == "ready",
            WorkflowStep.ready_at <= now,
            WorkflowRun.status.in_(("running", "waiting")),
        )
        .order_by(WorkflowStep.ready_at)
        .limit(limit)
    )
    return [(a, b, c) for a, b, c in rows.tuples()]


async def running_counts(
    session: AsyncSession,
) -> tuple[int, dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """Running steps: in all, per tenant and per run-as account (the steps loop's caps)."""
    rows = await session.execute(
        select(WorkflowStep.tenant_id, WorkflowRun.run_as_account_id, func.count())
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
        .where(WorkflowStep.status == "running")
        .group_by(WorkflowStep.tenant_id, WorkflowRun.run_as_account_id)
    )
    total, tenants, owners = 0, dict[uuid.UUID, int](), dict[uuid.UUID, int]()
    for tenant_id, owner, count in rows.tuples():
        total += count
        tenants[tenant_id] = tenants.get(tenant_id, 0) + count
        if owner is not None:
            owners[owner] = owners.get(owner, 0) + count
    return total, tenants, owners


async def claim_step(
    session: AsyncSession, *, step_id: uuid.UUID, owner: str, lease_until: datetime, now: datetime
) -> tuple[RunRow, StepRow, int] | None:
    """Lease a ready step: run row then step row, both SKIP LOCKED; None when taken.

    Returns the run, the leased step and the new attempt's number.
    """
    run_id = await session.scalar(select(WorkflowStep.run_id).where(WorkflowStep.id == step_id))
    if run_id is None:
        return None
    run = await get_run(session, run_id, lock=True, skip_locked=True)
    if run is None or run.status not in ("running", "waiting"):
        return None
    step = (
        await session.execute(
            select(WorkflowStep)
            .where(WorkflowStep.id == step_id, WorkflowStep.status == "ready")
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if step is None:
        return None
    number = (
        await session.scalar(
            select(func.coalesce(func.max(WorkflowAttempt.number), 0)).where(
                WorkflowAttempt.step_id == step_id
            )
        )
        or 0
    ) + 1
    await session.execute(
        update(WorkflowStep)
        .where(WorkflowStep.id == step_id)
        .values(
            status="running",
            lease_owner=owner,
            lease_expires_at=lease_until,
            generation=WorkflowStep.generation + 1,
            started_at=func.coalesce(WorkflowStep.started_at, now),
        )
    )
    session.add(
        WorkflowAttempt(tenant_id=run.tenant_id, step_id=step_id, number=number, started_at=now)
    )
    await session.flush()
    claimed = await get_step(session, step_id)
    assert claimed is not None
    return run, claimed, number


async def finish_attempt(
    session: AsyncSession,
    *,
    step_id: uuid.UUID,
    status: AttemptStatus,
    now: datetime,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    await session.execute(
        update(WorkflowAttempt)
        .where(WorkflowAttempt.step_id == step_id, WorkflowAttempt.status == "running")
        .values(status=status, ended_at=now, error_class=error_class, error_message=error_message)
    )


async def mark_send_intent(session: AsyncSession, *, step_id: uuid.UUID, now: datetime) -> None:
    """Executors call this before a write leaves Daimon: an attempt lost after it is `unknown`."""
    await session.execute(
        update(WorkflowAttempt)
        .where(WorkflowAttempt.step_id == step_id, WorkflowAttempt.status == "running")
        .values(send_intent_at=now)
    )


async def list_attempts(session: AsyncSession, step_id: uuid.UUID) -> list[AttemptRow]:
    rows = await session.scalars(
        select(WorkflowAttempt)
        .where(WorkflowAttempt.step_id == step_id)
        .order_by(WorkflowAttempt.number)
    )
    return [AttemptRow.model_validate(orm) for orm in rows]


async def renew_leases(
    session: AsyncSession, *, owner: str, held: Mapping[uuid.UUID, int], until: datetime
) -> set[uuid.UUID]:
    """Extend the leases `owner` still holds at those generations; the rest were lost."""
    if not held:
        return set()
    rows = await session.scalars(
        update(WorkflowStep)
        .where(
            WorkflowStep.lease_owner == owner,
            WorkflowStep.status == "running",
            func.row(WorkflowStep.id, WorkflowStep.generation).in_(
                [func.row(step_id, gen) for step_id, gen in held.items()]
            ),
        )
        .values(lease_expires_at=until)
        .returning(WorkflowStep.id)
    )
    return set(rows)


async def expired_leases(session: AsyncSession, *, now: datetime, limit: int) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(WorkflowStep.id)
        .where(WorkflowStep.status == "running", WorkflowStep.lease_expires_at < now)
        .order_by(WorkflowStep.lease_expires_at)
        .limit(limit)
    )
    return list(rows)


async def lock_step(session: AsyncSession, step_id: uuid.UUID) -> StepRow | None:
    """The step's row, locked; the caller locked its run first."""
    stmt = select(WorkflowStep).where(WorkflowStep.id == step_id).with_for_update()
    orm = (
        await session.execute(stmt.execution_options(populate_existing=True))
    ).scalar_one_or_none()
    return None if orm is None else StepRow.model_validate(orm)


async def run_of_step(session: AsyncSession, step_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(select(WorkflowStep.run_id).where(WorkflowStep.id == step_id))


# --- waits ---


async def open_wait(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    step_id: uuid.UUID,
    kind: str,
    expires_at: datetime | None,
    remind_at: datetime | None,
    now: datetime,
    shown: dict[str, Any] | None = None,
) -> WaitRow:
    orm = WorkflowWait(
        shown=shown,
        tenant_id=tenant_id,
        run_id=run_id,
        step_id=step_id,
        kind=kind,
        expires_at=expires_at,
        remind_at=remind_at,
        opened_at=now,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return WaitRow.model_validate(orm)


async def due_waits(
    session: AsyncSession, *, kind: str, now: datetime, limit: int
) -> list[WaitRow]:
    """Open waits of `kind` whose recheck or expiry is due."""
    rows = await session.scalars(
        select(WorkflowWait)
        .where(
            WorkflowWait.status == "waiting",
            WorkflowWait.kind == kind,
            (WorkflowWait.remind_at <= now) | (WorkflowWait.expires_at <= now),
        )
        .order_by(WorkflowWait.opened_at)
        .limit(limit)
    )
    return [WaitRow.model_validate(orm) for orm in rows]


async def set_remind_at(session: AsyncSession, wait_id: uuid.UUID, remind_at: datetime) -> None:
    await session.execute(
        update(WorkflowWait).where(WorkflowWait.id == wait_id).values(remind_at=remind_at)
    )


async def close_waits(
    session: AsyncSession,
    *,
    status: Literal["completed", "timed_out", "cancelled"],
    now: datetime,
    step_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Close the open waits of a step or a whole run; returns their ids."""
    where = [WorkflowWait.status == "waiting"]
    where.append(WorkflowWait.step_id == step_id if step_id else WorkflowWait.run_id == run_id)
    rows = await session.scalars(
        update(WorkflowWait)
        .where(and_(*where))
        .values(status=status, closed_at=now)
        .returning(WorkflowWait.id)
    )
    return list(rows)


# --- runs the loops pick up ---


async def count_active_runs(session: AsyncSession, *, workflow_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).where(
                WorkflowRun.workflow_id == workflow_id,
                WorkflowRun.status.in_(("running", "waiting")),
            )
        )
        or 0
    )


async def queued_runs(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(WorkflowRun.id)
        .where(WorkflowRun.status == "queued")
        .order_by(WorkflowRun.created_at)
        .limit(limit)
    )
    return list(rows)


async def overdue_runs(session: AsyncSession, *, now: datetime, limit: int) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(WorkflowRun.id)
        .where(WorkflowRun.status.in_(ACTIVE_RUN_STATES), WorkflowRun.deadline_at < now)
        .order_by(WorkflowRun.deadline_at)
        .limit(limit)
    )
    return list(rows)


async def over_budget_runs(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.status.in_(ACTIVE_RUN_STATES),
            WorkflowRun.cost_usd >= WorkflowRun.budget_usd,
        )
        .limit(limit)
    )
    return list(rows)


async def finished_runs_before(
    session: AsyncSession, *, tenant_id: uuid.UUID, before: datetime, limit: int
) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.tenant_id == tenant_id,
            WorkflowRun.status.not_in(ACTIVE_RUN_STATES),
            WorkflowRun.ended_at < before,
        )
        .limit(limit)
    )
    return list(rows)


async def tenants_with_finished_runs(session: AsyncSession) -> list[uuid.UUID]:
    rows = await session.scalars(
        select(WorkflowRun.tenant_id).where(WorkflowRun.status.not_in(ACTIVE_RUN_STATES)).distinct()
    )
    return list(rows)


async def delete_runs(session: AsyncSession, run_ids: Sequence[uuid.UUID]) -> None:
    """Delete runs with their steps, waits and ledger (the ledger guard lets a cascade pass)."""
    if run_ids:
        await session.execute(delete(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))


# --- trigger events and grants ---


class TriggerEventRow(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    trigger_id: uuid.UUID
    test: bool
    inputs: dict[str, Any] | None
    received_at: datetime


async def claim_trigger_events(
    session: AsyncSession, *, limit: int
) -> list[tuple[TriggerEventRow, uuid.UUID, str, str]]:
    """Accepted events, locked SKIP LOCKED: `(event, workflow id, trigger kind, trigger key)`."""
    rows = await session.execute(
        select(
            WorkflowTriggerEvent,
            WorkflowTrigger.workflow_id,
            WorkflowTrigger.kind,
            WorkflowTrigger.trigger_key,
        )
        .join(WorkflowTrigger, WorkflowTrigger.id == WorkflowTriggerEvent.trigger_id)
        .where(WorkflowTriggerEvent.status == "accepted")
        .order_by(WorkflowTriggerEvent.received_at)
        .limit(limit)
        .with_for_update(of=WorkflowTriggerEvent, skip_locked=True)
    )
    return [(TriggerEventRow.model_validate(e), w, k, key) for e, w, k, key in rows.tuples()]


async def mark_trigger_event(
    session: AsyncSession,
    event_id: uuid.UUID,
    *,
    status: Literal["started", "skipped", "rejected"],
    run_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    await session.execute(
        update(WorkflowTriggerEvent)
        .where(WorkflowTriggerEvent.id == event_id)
        .values(status=status, run_id=run_id, reason=reason)
    )


async def clear_payloads(session: AsyncSession, *, before: datetime) -> int:
    rows = await session.scalars(
        update(WorkflowTriggerEvent)
        .where(WorkflowTriggerEvent.received_at < before, WorkflowTriggerEvent.payload.is_not(None))
        .values(payload=None, headers=None)
        .returning(WorkflowTriggerEvent.id)
    )
    return len(rows.all())


class GrantRow(_Row):
    kind: str
    node_id: str | None
    pins: dict[str, Any] | None


async def live_grants(
    session: AsyncSession, *, workflow_id: uuid.UUID, version: int
) -> list[GrantRow]:
    rows = await session.scalars(
        select(WorkflowGrant).where(
            WorkflowGrant.workflow_id == workflow_id,
            WorkflowGrant.version == version,
            WorkflowGrant.revoked_at.is_(None),
        )
    )
    return [GrantRow.model_validate(orm) for orm in rows]
