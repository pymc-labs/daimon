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
    WorkflowLane,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepActivity,
    WorkflowWait,
)
from daimon.core.workflows.engine import ErrorClass, StepStatus
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

ACTIVE_RUN_STATES = ("queued", "running", "waiting")
ACTIVITY_LIMIT = 200
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
    concurrency_key: str | None
    parent_run_id: uuid.UUID | None
    #: The Workflow step (or For each item) this child run reports back to.
    parent_step_id: uuid.UUID | None
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
    #: A For each item: its index, its For each step, and `{"item": ..., "index": ...}`.
    item_index: int | None
    parent_step_id: uuid.UUID | None
    inputs: Any
    status: StepStatus
    waiting_reason: str | None
    output: Any
    files: list[Any] = []
    summary: str | None
    route: str | None
    error_class: ErrorClass | None
    error_message: str | None
    via: list[str]
    reused: Literal["fork", "pinned"] | None
    lane: str | None
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
    session_id: str | None
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
    files: list[Any]
    summary: str | None
    route: str | None
    error_class: str | None
    error_message: str | None
    via: list[str]
    reused: str | None
    #: Only set when the step is first written; `set_lane` refines it.
    lane: str | None
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
    concurrency_key: str | None = None,
    parent_run_id: uuid.UUID | None = None,
    parent_step_id: uuid.UUID | None = None,
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
        concurrency_key=concurrency_key,
        parent_run_id=parent_run_id,
        parent_step_id=parent_step_id,
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


async def carded_runs(
    session: AsyncSession, *, platform: str, ended_after: datetime, started_after: datetime
) -> list[RunRow]:
    """Runs started from a chat card on `platform` that are going or ended after `ended_after`."""
    card = WorkflowRun.triggered_by["card"]
    stmt = select(WorkflowRun).where(
        card["platform"].astext == platform,
        WorkflowRun.created_at >= started_after,
        (WorkflowRun.ended_at.is_(None)) | (WorkflowRun.ended_at >= ended_after),
    )
    return [RunRow.model_validate(orm) for orm in (await session.execute(stmt)).scalars()]


async def show_on_card(session: AsyncSession, run_id: uuid.UUID, card: Mapping[str, str]) -> None:
    """Name the chat message that shows the run, for the run card follower."""
    orm = await session.get(WorkflowRun, run_id, with_for_update=True)
    if orm is not None:
        orm.triggered_by = {**orm.triggered_by, "card": dict(card)}


async def update_run(
    session: AsyncSession, run_id: uuid.UUID, **changes: Unpack[RunChanges]
) -> None:
    await session.execute(update(WorkflowRun).where(WorkflowRun.id == run_id).values(**changes))


async def add_cost(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    step_id: uuid.UUID | None,
    cost_usd: Decimal,
    attempt_id: uuid.UUID | None = None,
) -> None:
    """Roll a debit up to the run, and to its step and attempt when known."""
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .values(cost_usd=WorkflowRun.cost_usd + cost_usd)
    )
    if step_id is not None:
        await session.execute(
            update(WorkflowStep)
            .where(WorkflowStep.id == step_id)
            .values(cost_usd=WorkflowStep.cost_usd + cost_usd)
        )
    if attempt_id is not None:
        await session.execute(
            update(WorkflowAttempt)
            .where(WorkflowAttempt.id == attempt_id)
            .values(cost_usd=WorkflowAttempt.cost_usd + cost_usd)
        )


async def attempt_of_session(
    session: AsyncSession, *, run_id: uuid.UUID, session_id: str, at: datetime
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """`(step id, attempt id)` that `session_id` was working on at `at`, in `run_id`.

    A lane's session serves several steps in turn; the latest attempt that started on it
    by then is the one a model call belongs to.
    """
    row = (
        await session.execute(
            select(WorkflowAttempt.step_id, WorkflowAttempt.id)
            .join(WorkflowStep, WorkflowStep.id == WorkflowAttempt.step_id)
            .where(
                WorkflowStep.run_id == run_id,
                WorkflowAttempt.session_id == session_id,
                WorkflowAttempt.started_at <= at,
            )
            .order_by(WorkflowAttempt.started_at.desc())
            .limit(1)
        )
    ).first()
    return None if row is None else (row[0], row[1])


async def list_steps(session: AsyncSession, run_id: uuid.UUID) -> list[StepRow]:
    """The run's node visits; For each items are `list_items`."""
    rows = await session.scalars(
        select(WorkflowStep)
        .where(WorkflowStep.run_id == run_id, WorkflowStep.item_index.is_(None))
        .order_by(WorkflowStep.created_at, WorkflowStep.node_id, WorkflowStep.visit)
        .execution_options(populate_existing=True)
    )
    return [StepRow.model_validate(orm) for orm in rows]


async def list_items(
    session: AsyncSession, *, run_id: uuid.UUID, parent_step_id: uuid.UUID | None = None
) -> list[StepRow]:
    """For each items of one For each step, or of the whole run, in item order."""
    where = [WorkflowStep.run_id == run_id, WorkflowStep.item_index.is_not(None)]
    if parent_step_id is not None:
        where.append(WorkflowStep.parent_step_id == parent_step_id)
    rows = await session.scalars(
        select(WorkflowStep)
        .where(*where)
        .order_by(WorkflowStep.created_at, WorkflowStep.item_index)
        .execution_options(populate_existing=True)
    )
    return [StepRow.model_validate(orm) for orm in rows]


async def save_items(
    session: AsyncSession,
    *,
    parent: StepRow,
    items: Mapping[int, StepWrite],
    inputs: Mapping[int, Any] | None = None,
    bump_generation: bool = False,
) -> None:
    """Insert or update the items of a For each step by index; the caller holds the run lock."""
    for index, write in items.items():
        values: dict[str, Any] = dict(write)
        if inputs is not None and index in inputs:
            values["inputs"] = inputs[index]
        stmt = insert(WorkflowStep).values(
            tenant_id=parent.tenant_id,
            run_id=parent.run_id,
            node_id=parent.node_id,
            visit=parent.visit,
            item_index=index,
            parent_step_id=parent.id,
            **values,
        )
        changes = dict(values)
        if bump_generation:
            changes["generation"] = WorkflowStep.generation + 1
        await session.execute(
            stmt.on_conflict_do_update(constraint="uq_workflow_steps_visit", set_=changes)
        )


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
        if "lane" in changes:
            changes["lane"] = func.coalesce(WorkflowStep.lane, stmt.excluded.lane)
        if bump_generation:
            changes["generation"] = WorkflowStep.generation + 1
        await session.execute(
            stmt.on_conflict_do_update(constraint="uq_workflow_steps_visit", set_=changes)
            if changes
            else stmt.on_conflict_do_nothing()
        )


async def ready_steps(
    session: AsyncSession, *, now: datetime, limit: int
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None, bool]]:
    """Due ready steps, oldest first: `(step id, tenant, run-as account, is an agent step)`."""
    rows = await session.execute(
        select(
            WorkflowStep.id,
            WorkflowStep.tenant_id,
            WorkflowRun.run_as_account_id,
            WorkflowStep.lane.is_not(None),
        )
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
        .where(
            WorkflowStep.status == "ready",
            WorkflowStep.ready_at <= now,
            WorkflowRun.status.in_(("running", "waiting")),
        )
        .order_by(WorkflowStep.ready_at)
        .limit(limit)
    )
    return [(a, b, c, d) for a, b, c, d in rows.tuples()]


async def running_counts(
    session: AsyncSession,
) -> tuple[int, dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """Running steps: in all, then agent steps (those with a lane) per tenant and per run-as
    account (the steps loop's caps)."""
    rows = await session.execute(
        select(
            WorkflowStep.tenant_id,
            WorkflowRun.run_as_account_id,
            func.count(),
            func.count().filter(WorkflowStep.lane.is_not(None)),
        )
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
        .where(WorkflowStep.status == "running")
        .group_by(WorkflowStep.tenant_id, WorkflowRun.run_as_account_id)
    )
    total, tenants, owners = 0, dict[uuid.UUID, int](), dict[uuid.UUID, int]()
    for tenant_id, owner, count, agents in rows.tuples():
        total += count
        tenants[tenant_id] = tenants.get(tenant_id, 0) + agents
        if owner is not None:
            owners[owner] = owners.get(owner, 0) + agents
    return total, tenants, owners


async def claim_step(
    session: AsyncSession, *, step_id: uuid.UUID, owner: str, lease_until: datetime, now: datetime
) -> tuple[RunRow, StepRow, int] | None:
    """Lease a ready step: run row then step row, both SKIP LOCKED; None when taken.

    Returns the run, the leased step and the attempt's number: a new attempt, or the one
    still running when a reclaimed agent step is reattached to its session.
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
    kept = await session.scalar(
        select(WorkflowAttempt.number).where(
            WorkflowAttempt.step_id == step_id, WorkflowAttempt.status == "running"
        )
    )
    number = (
        kept
        or (
            await session.scalar(
                select(func.coalesce(func.max(WorkflowAttempt.number), 0)).where(
                    WorkflowAttempt.step_id == step_id
                )
            )
            or 0
        )
        + 1
    )
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
    if kept is None:
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


async def running_attempt(session: AsyncSession, step_id: uuid.UUID) -> AttemptRow | None:
    orm = await session.scalar(
        select(WorkflowAttempt).where(
            WorkflowAttempt.step_id == step_id, WorkflowAttempt.status == "running"
        )
    )
    return None if orm is None else AttemptRow.model_validate(orm)


async def get_attempt(session: AsyncSession, attempt_id: uuid.UUID) -> AttemptRow | None:
    orm = await session.get(WorkflowAttempt, attempt_id)
    return None if orm is None else AttemptRow.model_validate(orm)


async def set_attempt_session(
    session: AsyncSession, *, attempt_id: uuid.UUID, session_id: str, now: datetime
) -> None:
    """Saved before the first send, so a lost worker's successor can reattach."""
    await session.execute(
        update(WorkflowAttempt)
        .where(WorkflowAttempt.id == attempt_id)
        .values(session_id=session_id, send_intent_at=now)
    )


async def mark_taint(session: AsyncSession, *, step_id: uuid.UUID, taint: str) -> None:
    """Add a JSON path to the step's untrusted paths."""
    current = await session.scalar(select(WorkflowStep.taint).where(WorkflowStep.id == step_id))
    if current is not None and taint not in current:
        await session.execute(
            update(WorkflowStep).where(WorkflowStep.id == step_id).values(taint=[*current, taint])
        )


async def set_lane(session: AsyncSession, *, step_id: uuid.UUID, lane: str) -> None:
    await session.execute(update(WorkflowStep).where(WorkflowStep.id == step_id).values(lane=lane))


async def lane_busy(
    session: AsyncSession, *, run_id: uuid.UUID, lane: str, step_id: uuid.UUID
) -> bool:
    """Whether another step of the run is running on `lane` (one turn at a time a session)."""
    other = await session.scalar(
        select(WorkflowStep.id).where(
            WorkflowStep.run_id == run_id,
            WorkflowStep.lane == lane,
            WorkflowStep.status == "running",
            WorkflowStep.id != step_id,
        )
    )
    return other is not None


async def get_lane(session: AsyncSession, *, run_id: uuid.UUID, lane: str) -> str | None:
    """The open session of a lane, if one was created."""
    return await session.scalar(
        select(WorkflowLane.session_id).where(
            WorkflowLane.run_id == run_id,
            WorkflowLane.lane_key == lane,
            WorkflowLane.status == "open",
        )
    )


async def put_lane(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    lane: str,
    agent_id: str,
    session_id: str | None,
) -> None:
    """Open (or replace) a lane's session; None archives it, so the next step starts fresh."""
    stmt = insert(WorkflowLane).values(
        tenant_id=tenant_id, run_id=run_id, lane_key=lane, agent_id=agent_id, session_id=session_id
    )
    await session.execute(
        stmt.on_conflict_do_update(
            constraint="uq_workflow_lanes_key",
            set_={
                "agent_id": agent_id,
                "session_id": session_id,
                "status": "open" if session_id else "archived",
            },
        )
    )


async def add_activity(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    step_id: uuid.UUID,
    kind: Literal["message", "tool", "note"],
    text: str,
    tool: str | None = None,
    now: datetime,
) -> None:
    """One line on the step's activity, bounded per step (`ACTIVITY_LIMIT`)."""
    count = await session.scalar(
        select(func.count()).where(WorkflowStepActivity.step_id == step_id)
    )
    if (count or 0) >= ACTIVITY_LIMIT:
        return
    session.add(
        WorkflowStepActivity(
            tenant_id=tenant_id, step_id=step_id, kind=kind, text=text[:500], tool=tool, at=now
        )
    )


async def list_activity(
    session: AsyncSession, step_id: uuid.UUID
) -> list[tuple[datetime, str, str, str | None]]:
    """The step's activity lines (at, kind, text, tool), oldest first."""
    rows = await session.execute(
        select(
            WorkflowStepActivity.at,
            WorkflowStepActivity.kind,
            WorkflowStepActivity.text,
            WorkflowStepActivity.tool,
        )
        .where(WorkflowStepActivity.step_id == step_id)
        .order_by(WorkflowStepActivity.id)
    )
    return list(rows.tuples())


async def list_attempts(session: AsyncSession, step_id: uuid.UUID) -> list[AttemptRow]:
    rows = await session.scalars(
        select(WorkflowAttempt)
        .where(WorkflowAttempt.step_id == step_id)
        .order_by(WorkflowAttempt.number)
    )
    return [AttemptRow.model_validate(orm) for orm in rows]


async def list_attempt_costs(
    session: AsyncSession, run_id: uuid.UUID
) -> list[tuple[str, int, int | None, datetime, Decimal]]:
    """Each attempt's step (node, visit, item), start and cost, oldest first."""
    rows = await session.execute(
        select(
            WorkflowStep.node_id,
            WorkflowStep.visit,
            WorkflowStep.item_index,
            WorkflowAttempt.started_at,
            WorkflowAttempt.cost_usd,
        )
        .join(WorkflowStep, WorkflowStep.id == WorkflowAttempt.step_id)
        .where(WorkflowStep.run_id == run_id)
        .order_by(WorkflowAttempt.started_at)
    )
    return list(rows.tuples())


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
    addressees: Sequence[str] = (),
    deliver: str | None = None,
    min_approvals: int = 1,
    event_source: str | None = None,
    event_key: str | None = None,
) -> WaitRow:
    orm = WorkflowWait(
        shown=shown,
        addressees=list(addressees),
        deliver=deliver,
        min_approvals=min_approvals,
        event_source=event_source,
        event_key=event_key,
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


async def count_active_runs(
    session: AsyncSession,
    *,
    workflow_id: uuid.UUID,
    concurrency_key: str | None = None,
    statuses: Sequence[str] = ("running", "waiting"),
) -> int:
    """Runs of the workflow in `statuses`; with a key, only the runs holding that key."""
    stmt = select(func.count()).where(
        WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(statuses)
    )
    if concurrency_key is not None:
        stmt = stmt.where(WorkflowRun.concurrency_key == concurrency_key)
    return await session.scalar(stmt) or 0


async def active_run_ids(
    session: AsyncSession, *, workflow_id: uuid.UUID, concurrency_key: str | None
) -> list[uuid.UUID]:
    stmt = select(WorkflowRun.id).where(
        WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(("running", "waiting"))
    )
    if concurrency_key is not None:
        stmt = stmt.where(WorkflowRun.concurrency_key == concurrency_key)
    return list(await session.scalars(stmt.order_by(WorkflowRun.created_at)))


async def latest_child(session: AsyncSession, step_id: uuid.UUID) -> RunRow | None:
    """The newest run a Workflow step (or item) started."""
    orm = await session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.parent_step_id == step_id)
        .order_by(WorkflowRun.created_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return None if orm is None else RunRow.model_validate(orm)


async def parents_of_ended_children(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Workflow steps waiting on a child run that has ended."""
    rows = await session.scalars(
        select(WorkflowStep.id)
        .join(WorkflowRun, WorkflowRun.parent_step_id == WorkflowStep.id)
        .where(
            WorkflowStep.status == "waiting",
            WorkflowStep.waiting_reason == "event",
            WorkflowRun.status.not_in(ACTIVE_RUN_STATES),
        )
        .distinct()
        .limit(limit)
    )
    return list(rows)


async def orphaned_children(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Active child runs whose calling step has ended (its run was cancelled or moved on)."""
    rows = await session.scalars(
        select(WorkflowRun.id)
        .join(WorkflowStep, WorkflowStep.id == WorkflowRun.parent_step_id)
        .where(
            WorkflowRun.status.in_(ACTIVE_RUN_STATES),
            WorkflowStep.status.in_(("succeeded", "failed", "skipped", "cancelled")),
        )
        .limit(limit)
    )
    return list(rows)


async def previous_end(
    session: AsyncSession, *, workflow_id: uuid.UUID, before: uuid.UUID
) -> str | None:
    """How the workflow's last other ended live run ended (cancelled runs left out)."""
    return await session.scalar(
        select(WorkflowRun.status)
        .where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.id != before,
            WorkflowRun.mode == "live",
            WorkflowRun.status.in_(("succeeded", "failed", "timed_out")),
        )
        .order_by(WorkflowRun.ended_at.desc().nulls_last(), WorkflowRun.number.desc())
        .limit(1)
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


# --- grants ---


class GrantRow(_Row):
    kind: str
    node_id: str | None
    pins: dict[str, Any] | None
    tool: str | None = None
    params: dict[str, Any] | None = None
    approved_by_account_id: uuid.UUID | None = None


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
