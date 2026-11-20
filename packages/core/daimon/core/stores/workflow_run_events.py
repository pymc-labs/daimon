"""Store for the run ledger: appends under the run row lock, so seq and the chain are gapless."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from daimon.core._models import WorkflowRun, WorkflowRunEvent
from daimon.core.workflows.ledger import GENESIS, event_hash
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


class EventRow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    run_id: uuid.UUID
    seq: int
    at: datetime
    type: str
    step_key: str | None
    actor: str
    data: dict[str, Any]
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class NewEvent:
    type: str
    step_key: str | None
    data: dict[str, Any]


async def append_events(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    events: Sequence[NewEvent],
    actor: str,
    at: datetime,
) -> str | None:
    """Append in order and return the new head; locks the run row until the transaction ends."""
    if not events:
        return None
    run = (
        await session.execute(
            select(WorkflowRun.tenant_id, WorkflowRun.last_seq, WorkflowRun.head_hash)
            .where(WorkflowRun.id == run_id)
            .with_for_update()
        )
    ).one()
    tenant_id, seq, head = run.tenant_id, run.last_seq, run.head_hash or GENESIS
    rows: list[WorkflowRunEvent] = []
    for e in events:
        seq += 1
        digest = event_hash(
            head,
            run_id=run_id,
            seq=seq,
            at=at,
            type=e.type,
            step=e.step_key,
            actor=actor,
            data=e.data,
        )
        rows.append(
            WorkflowRunEvent(
                run_id=run_id,
                seq=seq,
                tenant_id=tenant_id,
                at=at,
                type=e.type,
                step_key=e.step_key,
                actor=actor,
                data=e.data,
                prev_hash=head,
                hash=digest,
            )
        )
        head = digest
    session.add_all(rows)
    await session.execute(
        update(WorkflowRun).where(WorkflowRun.id == run_id).values(last_seq=seq, head_hash=head)
    )
    await session.flush()
    return head


async def list_events(
    session: AsyncSession, run_id: uuid.UUID, *, after_seq: int = 0
) -> list[EventRow]:
    rows = await session.scalars(
        select(WorkflowRunEvent)
        .where(WorkflowRunEvent.run_id == run_id, WorkflowRunEvent.seq > after_seq)
        .order_by(WorkflowRunEvent.seq)
    )
    return [EventRow.model_validate(orm) for orm in rows]


async def step_events(
    session: AsyncSession, run_id: uuid.UUID, *, step_key: str, types: Sequence[str]
) -> list[EventRow]:
    rows = await session.scalars(
        select(WorkflowRunEvent)
        .where(
            WorkflowRunEvent.run_id == run_id,
            WorkflowRunEvent.step_key == step_key,
            WorkflowRunEvent.type.in_(types),
        )
        .order_by(WorkflowRunEvent.seq)
    )
    return [EventRow.model_validate(orm) for orm in rows]
