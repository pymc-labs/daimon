"""Tenant-fenced background command records and their single completion wake."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import ComputeTaskRecord
from daimon.core.compute_models import (
    ACTIVE_TASK_STATES,
    ComputeSession,
    ComputeTask,
    ComputeTaskState,
)
from daimon.core.compute_tasks import wake_text
from daimon.core.errors import StoreError
from daimon.core.stores.compute_sessions import scope
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def get(db: AsyncSession, tenant_id: uuid.UUID, task_id: uuid.UUID) -> ComputeTask | None:
    await scope(db, tenant_id)
    row = await db.scalar(
        select(ComputeTaskRecord).where(
            ComputeTaskRecord.tenant_id == tenant_id, ComputeTaskRecord.id == task_id
        )
    )
    return None if row is None else ComputeTask.model_validate(row)


async def list_active(db: AsyncSession, tenant_id: uuid.UUID) -> list[ComputeTask]:
    await scope(db, tenant_id)
    rows = await db.scalars(
        select(ComputeTaskRecord).where(
            ComputeTaskRecord.tenant_id == tenant_id,
            ComputeTaskRecord.state.in_(ACTIVE_TASK_STATES),
        )
    )
    return [ComputeTask.model_validate(row) for row in rows]


async def register(db: AsyncSession, task: ComputeTask, *, cap: int) -> tuple[ComputeTask, bool]:
    """Return the existing row for a retried request, else admit under the session cap.

    Callers hold the session row lock, which serializes concurrent admissions.
    """
    await scope(db, task.tenant_id)
    existing = await db.scalar(
        select(ComputeTaskRecord).where(
            ComputeTaskRecord.tenant_id == task.tenant_id,
            ComputeTaskRecord.request_key == task.request_key,
        )
    )
    if existing is not None:
        if (existing.owner_account_id, existing.session_id) != (
            task.owner_account_id,
            task.session_id,
        ):
            raise StoreError("Command not found")
        return ComputeTask.model_validate(existing), False
    running = await db.scalar(
        select(func.count())
        .select_from(ComputeTaskRecord)
        .where(
            ComputeTaskRecord.tenant_id == task.tenant_id,
            ComputeTaskRecord.session_id == task.session_id,
            ComputeTaskRecord.state.in_(ACTIVE_TASK_STATES),
        )
    )
    if (running or 0) >= cap:
        raise StoreError("Too many commands are running on this machine. Wait for one to finish.")
    db.add(ComputeTaskRecord(**task.model_dump()))
    await db.flush()
    return task, True


async def advance(db: AsyncSession, task: ComputeTask, **values: object) -> ComputeTask | None:
    """Versioned update of an active task; None when another writer moved it first."""
    await scope(db, task.tenant_id)
    row = await db.scalar(
        update(ComputeTaskRecord)
        .where(
            ComputeTaskRecord.tenant_id == task.tenant_id,
            ComputeTaskRecord.id == task.id,
            ComputeTaskRecord.version == task.version,
            ComputeTaskRecord.state.in_(ACTIVE_TASK_STATES),
        )
        .values(**values, version=task.version + 1)
        .returning(ComputeTaskRecord)
    )
    return None if row is None else ComputeTask.model_validate(row)


async def finish(
    db: AsyncSession,
    task: ComputeTask,
    session: ComputeSession,
    *,
    state: ComputeTaskState,
    now: datetime,
    wake: bool,
    exit_code: int | None = None,
    output_bytes: int | None = None,
    output_tail: str | None = None,
) -> ComputeTask | None:
    """End the task and queue its one completion wake in the same transaction.

    The wake goes to the session's own conversation, which is the only one a
    task can be started from. ``wake`` is false when the owner observed the end
    themselves in that conversation, so they are not told twice.
    """
    if state in ACTIVE_TASK_STATES:
        raise ValueError("Finish requires a terminal state")
    if session.id != task.session_id or session.tenant_id != task.tenant_id:
        raise ValueError("Task and session do not match")
    done = await advance(
        db,
        task,
        state=state,
        exit_code=exit_code,
        finished_at=now,
        output_bytes=task.output_bytes if output_bytes is None else output_bytes,
        output_tail=task.output_tail if output_tail is None else output_tail,
    )
    if done is None or not wake:
        return done
    from daimon.core.stores.task_continuations import record_continuation

    await record_continuation(
        db,
        tenant_id=session.tenant_id,
        platform=session.platform,
        parent_channel_id=session.parent_channel_id,
        thread_id=session.thread_id,
        requester_account_id=session.owner_account_id,
        requester_external_user_id=session.owner_external_user_id,
        target_ma_agent_id=session.agent_id,
        target_name=session.agent_name,
        reason="timer",
        idempotency_key=uuid.uuid5(task.id, "finished"),
        available_at=now,
        attended=False,
        requested_work=wake_text(done),
    )
    return done
