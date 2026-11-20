"""Store for publish requests: a publish that waits for an admin to approve its grants.

One request is pending per workflow; a new one replaces it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from daimon.core._models import WorkflowPublishRequest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
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
