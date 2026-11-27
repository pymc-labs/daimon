"""Store for workflow posts (Finish results, post steps, asks, notices), settled at most once.

Same contract as the routine outbox (`stores.routines.claim_routine_deliveries`): a claim
whose lease ran out is settled `skipped/interrupted`, never handed out again, and pending
rows are claimed `FOR UPDATE SKIP LOCKED` by the poller for their workspace's platform.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from daimon.core._models import Tenant, WorkflowDelivery, WorkflowStep
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

DeliveryKind = Literal["finish", "post", "ask", "reminder", "ask_update", "notice"]
SettledStatus = Literal["sent", "skipped", "refused", "failed"]


class DeliveryRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    run_id: uuid.UUID | None
    wait_id: uuid.UUID | None
    #: The post step that queued it.
    step_id: uuid.UUID | None
    kind: str
    target: dict[str, Any]
    payload: dict[str, Any]
    status: str
    lease_owner: str | None
    lease_expires_at: datetime | None
    note: str | None
    created_at: datetime
    sent_at: datetime | None
    #: Where a sent post landed (`channel`, `thread`, `message`, `link`, `opened`).
    placed: dict[str, Any] | None = None


async def queue_delivery(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    run_id: uuid.UUID | None,
    kind: DeliveryKind,
    target: dict[str, Any],
    payload: dict[str, Any],
    wait_id: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
    status: Literal["pending", "skipped"] = "pending",
    note: str | None = None,
) -> uuid.UUID:
    """Queue a post; `skipped` settles it at once (a result the agent already posted)."""
    orm = WorkflowDelivery(
        status=status,
        note=note,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        run_id=run_id,
        wait_id=wait_id,
        step_id=step_id,
        kind=kind,
        target=target,
        payload=payload,
    )
    session.add(orm)
    await session.flush()
    return orm.id


async def claim_deliveries(
    session: AsyncSession,
    *,
    platform: str,
    owner: str,
    now: datetime,
    lease: timedelta,
    limit: int = 20,
) -> list[DeliveryRow]:
    """Claim pending posts for `platform`'s live workspaces, oldest first."""
    tenant_ids = select(Tenant.id).where(Tenant.platform == platform, Tenant.archived_at.is_(None))
    await session.execute(
        update(WorkflowDelivery)
        .where(
            WorkflowDelivery.status == "claimed",
            WorkflowDelivery.lease_expires_at < now,
            WorkflowDelivery.tenant_id.in_(tenant_ids),
        )
        .values(status="skipped", note="interrupted", lease_owner=None, lease_expires_at=None)
    )
    due = (
        select(WorkflowDelivery.id)
        .where(WorkflowDelivery.status == "pending", WorkflowDelivery.tenant_id.in_(tenant_ids))
        .order_by(WorkflowDelivery.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(
        update(WorkflowDelivery)
        .where(WorkflowDelivery.id.in_(due))
        .values(
            status="claimed",
            lease_owner=owner,
            lease_expires_at=now + lease,
            attempts=WorkflowDelivery.attempts + 1,
        )
        .returning(WorkflowDelivery)
        .execution_options(synchronize_session=False)
    )
    rows = [DeliveryRow.model_validate(orm) for orm in result.scalars().all()]
    await session.flush()
    return sorted(rows, key=lambda r: r.created_at)


async def still_claimed(
    session: AsyncSession, delivery_id: uuid.UUID, *, owner: str, now: datetime
) -> bool:
    """Whether `owner` still holds an unexpired claim, asked before each upload."""
    found = await session.scalar(
        select(WorkflowDelivery.id).where(
            WorkflowDelivery.id == delivery_id,
            WorkflowDelivery.status == "claimed",
            WorkflowDelivery.lease_owner == owner,
            WorkflowDelivery.lease_expires_at > now,
        )
    )
    return found is not None


async def settle_delivery(
    session: AsyncSession,
    delivery_id: uuid.UUID,
    *,
    owner: str,
    status: SettledStatus,
    now: datetime,
    note: str | None = None,
    placed: dict[str, Any] | None = None,
) -> DeliveryRow | None:
    """Finish a claim `owner` still holds; None when the claim was lost."""
    orm = (
        await session.execute(
            update(WorkflowDelivery)
            .where(
                WorkflowDelivery.id == delivery_id,
                WorkflowDelivery.status == "claimed",
                WorkflowDelivery.lease_owner == owner,
            )
            .values(
                status=status,
                note=note,
                sent_at=now if status == "sent" else None,
                placed=placed,
                lease_owner=None,
                lease_expires_at=None,
            )
            .returning(WorkflowDelivery)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    await session.flush()
    return None if orm is None else DeliveryRow.model_validate(orm)


async def deliveries_of_wait(session: AsyncSession, wait_id: uuid.UUID) -> list[DeliveryRow]:
    """A wait's asks, reminders, updates and notice, oldest first."""
    found = await session.scalars(
        select(WorkflowDelivery)
        .where(WorkflowDelivery.wait_id == wait_id)
        .order_by(WorkflowDelivery.created_at, WorkflowDelivery.id)
    )
    return [DeliveryRow.model_validate(o) for o in found]


async def unsent_routine_posts(session: AsyncSession, workflow_id: uuid.UUID) -> list[DeliveryRow]:
    """A workflow's `post` and `notice` deliveries not yet sent (pending or claimed), oldest
    first."""
    found = await session.scalars(
        select(WorkflowDelivery)
        .where(
            WorkflowDelivery.workflow_id == workflow_id,
            WorkflowDelivery.kind.in_(("post", "notice")),
            WorkflowDelivery.status.in_(("pending", "claimed")),
        )
        .order_by(WorkflowDelivery.created_at, WorkflowDelivery.id)
    )
    return [DeliveryRow.model_validate(o) for o in found]


async def post_of_step(session: AsyncSession, step_id: uuid.UUID) -> DeliveryRow | None:
    """The post a post step queued, so a step claimed again does not post twice."""
    orm = await session.scalar(
        select(WorkflowDelivery).where(
            WorkflowDelivery.step_id == step_id, WorkflowDelivery.kind == "post"
        )
    )
    return None if orm is None else DeliveryRow.model_validate(orm)


async def settled_post_steps(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Post steps still waiting on a post that has settled."""
    rows = await session.scalars(
        select(WorkflowStep.id)
        .join(WorkflowDelivery, WorkflowDelivery.step_id == WorkflowStep.id)
        .where(
            WorkflowStep.status == "waiting",
            WorkflowDelivery.kind == "post",
            WorkflowDelivery.status.not_in(("pending", "claimed")),
        )
        .limit(limit)
    )
    return list(rows)


async def opened_threads(session: AsyncSession, run_id: uuid.UUID) -> set[str]:
    """Threads this run's own posts opened or found by title."""
    rows = await session.scalars(
        select(WorkflowDelivery.placed["thread"].astext).where(
            WorkflowDelivery.run_id == run_id,
            WorkflowDelivery.placed["opened"].as_boolean().is_(True),
        )
    )
    return {r for r in rows if r}
