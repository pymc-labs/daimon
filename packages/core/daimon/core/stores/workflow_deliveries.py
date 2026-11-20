"""Store for workflow posts: Finish results and owner notices, claimed and settled at most once.

Same contract as the routine outbox (`stores.routines.claim_routine_deliveries`): a claim
whose lease ran out is settled `skipped/interrupted`, never handed out again, and pending
rows are claimed `FOR UPDATE SKIP LOCKED` by the poller for their workspace's platform.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from daimon.core._models import Tenant, WorkflowDelivery
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

DeliveryKind = Literal["finish", "ask", "reminder", "ask_update", "notice"]
SettledStatus = Literal["sent", "skipped", "refused", "failed"]


class DeliveryRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    run_id: uuid.UUID | None
    wait_id: uuid.UUID | None
    kind: str
    target: dict[str, Any]
    payload: dict[str, Any]
    status: str
    lease_owner: str | None
    lease_expires_at: datetime | None
    note: str | None
    created_at: datetime
    sent_at: datetime | None


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
) -> uuid.UUID:
    orm = WorkflowDelivery(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        run_id=run_id,
        wait_id=wait_id,
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
                lease_owner=None,
                lease_expires_at=None,
            )
            .returning(WorkflowDelivery)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    await session.flush()
    return None if orm is None else DeliveryRow.model_validate(orm)


async def get_last_delivery(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> DeliveryRow | None:
    """The workflow's latest Finish post, for the run page's "last delivery"."""
    orm = await session.scalar(
        select(WorkflowDelivery)
        .where(
            WorkflowDelivery.tenant_id == tenant_id,
            WorkflowDelivery.workflow_id == workflow_id,
            WorkflowDelivery.kind == "finish",
        )
        .order_by(WorkflowDelivery.created_at.desc(), WorkflowDelivery.id.desc())
        .limit(1)
    )
    return None if orm is None else DeliveryRow.model_validate(orm)


async def deliveries_of_wait(session: AsyncSession, wait_id: uuid.UUID) -> list[DeliveryRow]:
    """A wait's asks, reminders, updates and notice, oldest first."""
    found = await session.scalars(
        select(WorkflowDelivery)
        .where(WorkflowDelivery.wait_id == wait_id)
        .order_by(WorkflowDelivery.created_at, WorkflowDelivery.id)
    )
    return [DeliveryRow.model_validate(o) for o in found]
