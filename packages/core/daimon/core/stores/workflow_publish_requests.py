"""Store for publish requests: a publish that waits for an admin to approve its grants.

One request is pending per workflow; a new one replaces it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from daimon.core._models import WorkflowGrant, WorkflowPublishRequest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession


class PublishRequestRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    workflow_id: uuid.UUID
    revision: int
    spec: dict[str, Any]
    note: str
    grants: list[Any]
    status: str
    version: int | None
    requested_by_account_id: uuid.UUID | None
    requested_at: datetime
    decided_by_account_id: uuid.UUID | None
    decided_at: datetime | None
    comment: str | None


async def open_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    revision: int,
    spec: dict[str, Any],
    note: str,
    grants: list[dict[str, Any]],
    requested_by: uuid.UUID,
) -> PublishRequestRow:
    """Ask for approval of `spec`, replacing the workflow's pending request."""
    await session.execute(
        delete(WorkflowPublishRequest).where(
            WorkflowPublishRequest.workflow_id == workflow_id,
            WorkflowPublishRequest.status == "pending",
        )
    )
    orm = WorkflowPublishRequest(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        revision=revision,
        spec=spec,
        note=note,
        grants=grants,
        requested_by_account_id=requested_by,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return PublishRequestRow.model_validate(orm)


async def pending_request(
    session: AsyncSession, *, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> PublishRequestRow | None:
    orm = (
        await session.execute(
            select(WorkflowPublishRequest).where(
                WorkflowPublishRequest.tenant_id == tenant_id,
                WorkflowPublishRequest.workflow_id == workflow_id,
                WorkflowPublishRequest.status == "pending",
            )
        )
    ).scalar_one_or_none()
    return None if orm is None else PublishRequestRow.model_validate(orm)


async def get_request(
    session: AsyncSession, *, tenant_id: uuid.UUID, request_id: uuid.UUID, lock: bool = False
) -> PublishRequestRow | None:
    stmt = select(WorkflowPublishRequest).where(
        WorkflowPublishRequest.tenant_id == tenant_id, WorkflowPublishRequest.id == request_id
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    orm = await session.scalar(stmt)
    return None if orm is None else PublishRequestRow.model_validate(orm)


async def inbox_requests(
    session: AsyncSession, *, tenant_id: uuid.UUID, decided_since: datetime
) -> list[PublishRequestRow]:
    """Pending requests, and those decided since `decided_since`, newest first."""
    rows = await session.scalars(
        select(WorkflowPublishRequest)
        .where(
            WorkflowPublishRequest.tenant_id == tenant_id,
            or_(
                WorkflowPublishRequest.status == "pending",
                WorkflowPublishRequest.decided_at >= decided_since,
            ),
        )
        .order_by(WorkflowPublishRequest.requested_at.desc())
        .limit(100)
    )
    return [PublishRequestRow.model_validate(r) for r in rows]


async def count_pending(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(WorkflowPublishRequest)
            .where(
                WorkflowPublishRequest.tenant_id == tenant_id,
                WorkflowPublishRequest.status == "pending",
            )
        )
        or 0
    )


async def decide_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    status: str,
    by: uuid.UUID,
    comment: str | None,
    version: int | None,
    now: datetime,
) -> None:
    await session.execute(
        update(WorkflowPublishRequest)
        .where(WorkflowPublishRequest.id == request_id)
        .values(
            status=status,
            decided_by_account_id=by,
            decided_at=now,
            comment=comment,
            version=version,
        )
    )


async def add_grants(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    version: int,
    grants: Sequence[Mapping[str, Any]],
    approved_by: uuid.UUID,
    now: datetime,
) -> None:
    """The grants an admin approved for one version: kind, node and pins."""
    for g in grants:
        session.add(
            WorkflowGrant(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                version=version,
                kind=g["kind"],
                node_id=g.get("node_id"),
                tool=g.get("tool"),
                params=dict(g.get("params") or {}),
                pins=dict(g.get("pins") or {}),
                approved_by_account_id=approved_by,
                approved_at=now,
            )
        )
    await session.flush()
