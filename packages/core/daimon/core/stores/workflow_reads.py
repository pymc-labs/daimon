"""Read queries behind the workflows pages: lists, tallies and what needs a person.

Row stores (`workflows`, `workflow_runs`) hold single-row reads; these join and count across
workflows so a list costs a few queries however many workflows it shows.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal

from daimon.core._models import PlatformPrincipal, WorkflowGrant, WorkflowRun, WorkflowStep
from daimon.core.stores.workflow_runs import ACTIVE_RUN_STATES, RunRow, StepRow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def recent_runs(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_ids: Collection[uuid.UUID], per: int
) -> dict[uuid.UUID, list[RunRow]]:
    """The last `per` live runs of each workflow, newest first."""
    if not workflow_ids:
        return {}
    rank = (
        func.row_number()
        .over(partition_by=WorkflowRun.workflow_id, order_by=WorkflowRun.number.desc())
        .label("rank")
    )
    ranked = (
        select(WorkflowRun.id, rank)
        .where(
            WorkflowRun.tenant_id == tenant_id,
            WorkflowRun.workflow_id.in_(list(workflow_ids)),
            WorkflowRun.mode == "live",
        )
        .subquery()
    )
    rows = await session.scalars(
        select(WorkflowRun)
        .join(ranked, ranked.c.id == WorkflowRun.id)
        .where(ranked.c.rank <= per)
        .order_by(WorkflowRun.workflow_id, WorkflowRun.number.desc())
    )
    out: dict[uuid.UUID, list[RunRow]] = defaultdict(list)
    for orm in rows:
        out[orm.workflow_id].append(RunRow.model_validate(orm))
    return out


async def spend_since(
    session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime
) -> dict[uuid.UUID, Decimal]:
    """What each workflow's runs cost since `since`, test runs included."""
    rows = await session.execute(
        select(WorkflowRun.workflow_id, func.sum(WorkflowRun.cost_usd))
        .where(WorkflowRun.tenant_id == tenant_id, WorkflowRun.created_at >= since)
        .group_by(WorkflowRun.workflow_id)
    )
    return {workflow_id: total or Decimal(0) for workflow_id, total in rows.tuples()}


async def list_runs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[RunRow]:
    """Runs, newest first; one workflow's, or the workspace's."""
    query = select(WorkflowRun).where(WorkflowRun.tenant_id == tenant_id)
    if workflow_id is not None:
        query = query.where(WorkflowRun.workflow_id == workflow_id)
    rows = await session.scalars(query.order_by(WorkflowRun.created_at.desc()).limit(limit))
    return [RunRow.model_validate(orm) for orm in rows]


async def run_by_number(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID, number: int
) -> RunRow | None:
    orm = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.workflow_id == workflow_id,
                WorkflowRun.number == number,
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else RunRow.model_validate(orm)


async def current_steps(
    session: AsyncSession, run_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[StepRow]]:
    """The steps of these runs that are running, waiting or unknown, oldest first."""
    if not run_ids:
        return {}
    rows = await session.scalars(
        select(WorkflowStep)
        .where(
            WorkflowStep.run_id.in_(list(run_ids)),
            WorkflowStep.status.in_(("running", "waiting", "unknown")),
        )
        .order_by(WorkflowStep.created_at)
    )
    out: dict[uuid.UUID, list[StepRow]] = defaultdict(list)
    for orm in rows:
        out[orm.run_id].append(StepRow.model_validate(orm))
    return out


async def unknown_step_runs(session: AsyncSession, *, tenant_id: uuid.UUID) -> list[RunRow]:
    """Active runs with a step whose outcome nobody knows."""
    rows = await session.scalars(
        select(WorkflowRun)
        .join(WorkflowStep, WorkflowStep.run_id == WorkflowRun.id)
        .where(
            WorkflowRun.tenant_id == tenant_id,
            WorkflowRun.status.in_(ACTIVE_RUN_STATES),
            WorkflowStep.status == "unknown",
        )
        .distinct()
    )
    return [RunRow.model_validate(orm) for orm in rows]


async def runs_per_version(session: AsyncSession, *, workflow_id: uuid.UUID) -> dict[int, int]:
    rows = await session.execute(
        select(WorkflowRun.version, func.count())
        .where(WorkflowRun.workflow_id == workflow_id, WorkflowRun.mode == "live")
        .group_by(WorkflowRun.version)
    )
    return {version: count for version, count in rows.tuples()}


async def grant_kinds_by_version(
    session: AsyncSession, *, workflow_id: uuid.UUID
) -> dict[int, list[str]]:
    """The kinds of grant each version holds, revoked ones left out."""
    rows = await session.execute(
        select(WorkflowGrant.version, WorkflowGrant.kind)
        .where(WorkflowGrant.workflow_id == workflow_id, WorkflowGrant.revoked_at.is_(None))
        .distinct()
    )
    out: dict[int, list[str]] = defaultdict(list)
    for version, kind in rows.tuples():
        out[version].append(kind)
    return out


async def platforms_by_account(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> dict[uuid.UUID, list[str]]:
    """The chat platforms each account of the workspace is known on."""
    rows = await session.execute(
        select(PlatformPrincipal.account_id, PlatformPrincipal.platform)
        .where(PlatformPrincipal.tenant_id == tenant_id)
        .distinct()
        .order_by(PlatformPrincipal.platform)
    )
    out: dict[uuid.UUID, list[str]] = defaultdict(list)
    for account_id, platform in rows.tuples():
        out[account_id].append(platform)
    return out
