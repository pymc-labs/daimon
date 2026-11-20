"""Store for wait decisions, the inbox reads and the event buffer.

Opening and closing waits with their steps is `workflow_runs`; this module holds a wait's
answers (one per person, deduplicated by idempotency key), its decided state, where its
asks were posted on chat, and events held for event waits. Lock order stays run, wait, step.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from daimon.core._models import (
    WorkflowEventBuffer,
    WorkflowWait,
    WorkflowWaitAnswer,
    WorkflowWaitPost,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


class _Row(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)


class Wait(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    step_id: uuid.UUID
    kind: str
    status: str
    addressees: list[Any]
    deliver: str | None
    min_approvals: int
    shown: dict[str, Any] | None
    #: What an approval with `binds` covers: the digest of `shown["binds"]` at approval.
    shown_hash: str | None = None
    event_source: str | None
    event_key: str | None
    expires_at: datetime | None
    remind_at: datetime | None
    decision: str | None
    decided_by_account_id: uuid.UUID | None
    decided_via: str | None
    comment: str | None
    answer_text: str | None
    answer_values: dict[str, Any] | None
    answer_files: list[Any] | None
    opened_at: datetime
    closed_at: datetime | None


class BufferedEvent(_Row):
    id: uuid.UUID
    source: str
    event_key: str | None
    payload: dict[str, Any]
    received_at: datetime


async def get_wait(
    session: AsyncSession, wait_id: uuid.UUID, *, tenant_id: uuid.UUID | None = None
) -> Wait | None:
    stmt = select(WorkflowWait).where(WorkflowWait.id == wait_id)
    if tenant_id is not None:
        stmt = stmt.where(WorkflowWait.tenant_id == tenant_id)
    orm = await session.scalar(stmt)
    return Wait.model_validate(orm) if orm else None


async def lock_wait(session: AsyncSession, wait_id: uuid.UUID) -> Wait | None:
    """The wait, row locked; take the run's lock first."""
    stmt = select(WorkflowWait).where(WorkflowWait.id == wait_id).with_for_update()
    orm = await session.scalar(stmt.execution_options(populate_existing=True))
    return Wait.model_validate(orm) if orm else None


async def waits_of_step(session: AsyncSession, step_id: uuid.UUID) -> list[Wait]:
    rows = await session.scalars(
        select(WorkflowWait).where(WorkflowWait.step_id == step_id).order_by(WorkflowWait.opened_at)
    )
    return [Wait.model_validate(r) for r in rows]


async def waits_by_id(
    session: AsyncSession, wait_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Wait]:
    if not wait_ids:
        return {}
    rows = await session.scalars(select(WorkflowWait).where(WorkflowWait.id.in_(wait_ids)))
    return {r.id: Wait.model_validate(r) for r in rows}


async def add_answer(
    session: AsyncSession,
    *,
    wait: Wait,
    account_id: uuid.UUID,
    route: str,
    via: str,
    comment: str | None,
    idempotency_key: str | None,
    now: datetime,
) -> bool:
    """Record one person's answer; False when they (or this key) answered already."""
    inserted = await session.scalar(
        insert(WorkflowWaitAnswer)
        .values(
            tenant_id=wait.tenant_id,
            wait_id=wait.id,
            account_id=account_id,
            route=route,
            via=via,
            comment=comment,
            idempotency_key=idempotency_key,
            answered_at=now,
        )
        .on_conflict_do_nothing()
        .returning(WorkflowWaitAnswer.id)
    )
    return inserted is not None


async def count_answers(session: AsyncSession, wait_id: uuid.UUID, route: str) -> int:
    """Distinct people who answered `route`."""
    found = await session.scalar(
        select(func.count(func.distinct(WorkflowWaitAnswer.account_id))).where(
            WorkflowWaitAnswer.wait_id == wait_id, WorkflowWaitAnswer.route == route
        )
    )
    return found or 0


async def decide(
    session: AsyncSession,
    wait_id: uuid.UUID,
    *,
    status: str,
    decision: str | None,
    by: uuid.UUID | None,
    via: str | None,
    comment: str | None = None,
    text: str | None = None,
    values: dict[str, Any] | None = None,
    files: list[Any] | None = None,
    shown: dict[str, Any] | None = None,
    shown_hash: str | None = None,
    now: datetime,
) -> None:
    """Close the wait; `shown` and `shown_hash` replace what it showed when given."""
    covered = {} if shown is None else {"shown": shown, "shown_hash": shown_hash}
    await session.execute(
        update(WorkflowWait)
        .where(WorkflowWait.id == wait_id)
        .values(
            **covered,
            status=status,
            decision=decision,
            decided_by_account_id=by,
            decided_via=via,
            comment=comment,
            answer_text=text,
            answer_values=values,
            answer_files=files,
            closed_at=now,
        )
    )


async def set_remind_at(
    session: AsyncSession, wait_id: uuid.UUID, remind_at: datetime | None
) -> None:
    await session.execute(
        update(WorkflowWait).where(WorkflowWait.id == wait_id).values(remind_at=remind_at)
    )


async def inbox_waits(
    session: AsyncSession, *, tenant_id: uuid.UUID, closed_since: datetime, limit: int = 200
) -> list[Wait]:
    """Approvals and questions still open, or closed since `closed_since`, newest first."""
    rows = await session.scalars(
        select(WorkflowWait)
        .where(
            WorkflowWait.tenant_id == tenant_id,
            WorkflowWait.kind.in_(("approval", "question")),
            or_(WorkflowWait.status == "waiting", WorkflowWait.closed_at >= closed_since),
        )
        .order_by(WorkflowWait.opened_at.desc())
        .limit(limit)
    )
    return [Wait.model_validate(r) for r in rows]


async def open_event_waits(session: AsyncSession, *, limit: int) -> list[Wait]:
    rows = await session.scalars(
        select(WorkflowWait)
        .where(WorkflowWait.status == "waiting", WorkflowWait.kind == "event")
        .order_by(WorkflowWait.opened_at)
        .limit(limit)
    )
    return [Wait.model_validate(r) for r in rows]


# --- the event buffer ---


async def buffer_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    source: str,
    event_key: str | None,
    payload: dict[str, Any],
    now: datetime,
    expires_at: datetime,
) -> None:
    session.add(
        WorkflowEventBuffer(
            tenant_id=tenant_id,
            source=source,
            event_key=event_key,
            payload=payload,
            received_at=now,
            expires_at=expires_at,
        )
    )
    await session.flush()


async def buffered_for(session: AsyncSession, wait: Wait, *, now: datetime) -> list[BufferedEvent]:
    """Unexpired events for the wait's source (and key, when it has one), oldest first."""
    assert wait.event_source is not None
    stmt = select(WorkflowEventBuffer).where(
        WorkflowEventBuffer.tenant_id == wait.tenant_id,
        WorkflowEventBuffer.source == wait.event_source,
        WorkflowEventBuffer.expires_at > now,
    )
    if wait.event_key is not None:
        stmt = stmt.where(WorkflowEventBuffer.event_key == wait.event_key)
    rows = await session.scalars(stmt.order_by(WorkflowEventBuffer.received_at).limit(50))
    return [BufferedEvent.model_validate(r) for r in rows]


async def take_event(session: AsyncSession, event_id: uuid.UUID) -> bool:
    """Consume a buffered event; False when another wait took it first."""
    gone = await session.scalar(
        delete(WorkflowEventBuffer)
        .where(WorkflowEventBuffer.id == event_id)
        .returning(WorkflowEventBuffer.id)
    )
    return gone is not None


async def source_used(session: AsyncSession, *, tenant_id: uuid.UUID, source: str) -> bool:
    """An event for `source` is held, or a wait on it was already answered."""
    held = await session.scalar(
        select(WorkflowEventBuffer.id).where(
            WorkflowEventBuffer.tenant_id == tenant_id, WorkflowEventBuffer.source == source
        )
    )
    taken = await session.scalar(
        select(WorkflowWait.id).where(
            WorkflowWait.tenant_id == tenant_id,
            WorkflowWait.event_source == source,
            WorkflowWait.status == "completed",
        )
    )
    return held is not None or taken is not None


async def drop_expired_events(session: AsyncSession, *, now: datetime) -> None:
    await session.execute(delete(WorkflowEventBuffer).where(WorkflowEventBuffer.expires_at <= now))


class WaitPost(_Row):
    id: uuid.UUID
    tenant_id: uuid.UUID
    wait_id: uuid.UUID
    platform: str
    channel_id: str
    thread_id: str | None
    message_id: str
    addressee_account_id: uuid.UUID | None
    created_at: datetime


async def record_post(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    wait_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None,
    message_id: str,
    addressee_account_id: uuid.UUID | None,
) -> WaitPost:
    orm = WorkflowWaitPost(
        tenant_id=tenant_id,
        wait_id=wait_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        message_id=message_id,
        addressee_account_id=addressee_account_id,
    )
    session.add(orm)
    await session.flush()
    return WaitPost.model_validate(orm)


async def posts_of_wait(session: AsyncSession, wait_id: uuid.UUID) -> list[WaitPost]:
    found = await session.scalars(
        select(WorkflowWaitPost)
        .where(WorkflowWaitPost.wait_id == wait_id)
        .order_by(WorkflowWaitPost.created_at, WorkflowWaitPost.id)
    )
    return [WaitPost.model_validate(o) for o in found]


async def post_by_message(
    session: AsyncSession, *, platform: str, message_id: str
) -> tuple[WaitPost, Wait] | None:
    """The ask posted as `message_id`, whatever its wait's state: its buttons' presses."""
    row = (
        await session.execute(
            select(WorkflowWaitPost, WorkflowWait)
            .join(WorkflowWait, WorkflowWait.id == WorkflowWaitPost.wait_id)
            .where(
                WorkflowWaitPost.platform == platform,
                WorkflowWaitPost.message_id == message_id,
            )
            .limit(1)
        )
    ).first()
    return None if row is None else (WaitPost.model_validate(row[0]), Wait.model_validate(row[1]))


async def open_post_for(
    session: AsyncSession,
    *,
    tenant_ids: Sequence[uuid.UUID],
    platform: str,
    thread_id: str | None,
    reply_to: str | None,
) -> tuple[WaitPost, Wait] | None:
    """The open ask a message answers: posted into its thread, or the message it replies to."""
    for column, value in (
        (WorkflowWaitPost.thread_id, thread_id),
        (WorkflowWaitPost.message_id, reply_to),
    ):
        if value is None:
            continue
        row = (
            await session.execute(
                select(WorkflowWaitPost, WorkflowWait)
                .join(WorkflowWait, WorkflowWait.id == WorkflowWaitPost.wait_id)
                .where(
                    WorkflowWaitPost.tenant_id.in_(tenant_ids),
                    WorkflowWaitPost.platform == platform,
                    column == value,
                    WorkflowWait.status == "waiting",
                )
                .order_by(WorkflowWaitPost.created_at.desc())
                .limit(1)
            )
        ).first()
        if row is not None:
            return WaitPost.model_validate(row[0]), Wait.model_validate(row[1])
    return None
