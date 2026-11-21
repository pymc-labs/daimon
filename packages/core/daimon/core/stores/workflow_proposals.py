"""Store for writes an agent step proposed: recorded during the turn, decided in the inbox,
sent by the step afterwards."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from daimon.core._models import WorkflowProposal
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

#: Proposals one step may record; more are refused to the agent.
MAX_PER_STEP: Final = 20
_TICK: Final = timedelta(microseconds=1)

Status = Literal["proposed", "approved", "rejected", "sent", "failed", "unknown"]


class Proposal(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    step_id: uuid.UUID
    server_url: str
    #: `server/tool`.
    tool: str
    arguments: dict[str, Any]
    arguments_hash: str
    reason: str | None
    status: Status
    result: dict[str, Any] | None
    decided_by_account_id: uuid.UUID | None
    created_at: datetime


def arguments_hash(arguments: dict[str, Any]) -> str:
    blob = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


async def proposals_of(session: AsyncSession, step_id: uuid.UUID) -> list[Proposal]:
    rows = await session.scalars(
        select(WorkflowProposal)
        .where(WorkflowProposal.step_id == step_id)
        .order_by(WorkflowProposal.created_at, WorkflowProposal.id)
    )
    return [Proposal.model_validate(r) for r in rows]


async def add_proposal(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    step_id: uuid.UUID,
    server_url: str,
    tool: str,
    arguments: dict[str, Any],
    reason: str | None,
) -> tuple[int, Proposal] | None:
    """Record a proposal (the same call twice is one); its 1-based number, or None when the
    step has proposed its maximum."""
    digest = arguments_hash(arguments)
    existing = await proposals_of(session, step_id)
    for n, p in enumerate(existing, 1):
        if p.tool == tool and p.arguments_hash == digest and p.server_url == server_url:
            return n, p
    if len(existing) >= MAX_PER_STEP:
        return None
    now = datetime.now(UTC)
    row = WorkflowProposal(
        tenant_id=tenant_id,
        step_id=step_id,
        server_url=server_url,
        tool=tool,
        arguments=arguments,
        arguments_hash=digest,
        reason=reason,
        # Proposals made in one transaction would share `now()`; keep them in order.
        created_at=max(now, existing[-1].created_at + _TICK) if existing else now,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return len(existing) + 1, Proposal.model_validate(row)


async def decide_proposals(
    session: AsyncSession,
    step_id: uuid.UUID,
    *,
    approved: Collection[uuid.UUID],
    by: uuid.UUID | None,
    now: datetime,
) -> None:
    """Approve the listed proposals of the step and reject its other open ones."""
    for status, chosen in (("approved", True), ("rejected", False)):
        picked = WorkflowProposal.id.in_(list(approved))
        await session.execute(
            update(WorkflowProposal)
            .where(
                WorkflowProposal.step_id == step_id,
                WorkflowProposal.status == "proposed",
                picked if chosen else ~picked,
            )
            .values(status=status, decided_by_account_id=by, decided_at=now)
        )


async def settle_proposal(
    session: AsyncSession,
    proposal_id: uuid.UUID,
    *,
    status: Status,
    result: dict[str, Any] | None,
    now: datetime,
) -> None:
    await session.execute(
        update(WorkflowProposal)
        .where(WorkflowProposal.id == proposal_id)
        .values(status=status, result=result, sent_at=func.coalesce(WorkflowProposal.sent_at, now))
    )
