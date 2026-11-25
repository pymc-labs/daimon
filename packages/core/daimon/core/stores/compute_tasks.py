"""Tenant-fenced background command records and their single completion wake."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core._models import ComputeTaskRecord, TaskContinuation
from daimon.core.access_policy import CHANNEL_SCOPE_PREFIX, DM_SCOPE_PREFIX
from daimon.core.compute_models import (
    ACTIVE_TASK_STATES,
    ComputeSession,
    ComputeTask,
    ComputeTaskState,
)
from daimon.core.compute_tasks import wake_key, wake_text
from daimon.core.errors import StoreError
from daimon.core.stores.compute_sessions import scope
from daimon.core.stores.domain import TurnOriginRow
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
        idempotency_key=wake_key(task.id),
        available_at=now,
        attended=False,
        requested_work=wake_text(done, reads=wake_reads_in(session)),
    )
    return done


def wake_reads_in(session: ComputeSession) -> bool:
    """Whether a finished command's wake can read this session: in a thread, yes.

    A session launched in a Discord DM or under a plain channel's scope gets its wake
    through the scope timer runners (`direct_messages.run_dm_timer`,
    `run_channel_timer`), which start the turn in a fresh scope. That is not the
    session's conversation, so every read is refused there
    (`packages/core/tests/test_compute_wake_scope_gap.py`).
    """
    return not session.thread_id.startswith((DM_SCOPE_PREFIX, CHANNEL_SCOPE_PREFIX))


async def wake_turn_of(
    db: AsyncSession, origin: TurnOriginRow, session: ComputeSession, *, now: datetime
) -> bool:
    """Whether ``origin`` is the turn a wake of one of this session's finished commands started.

    A dispatcher claims a wake row under a fresh ``lease_owner``, and the turn it
    then starts takes that owner as its origin id (`daimon.core.turn_origin.wake_turn`).
    So the row names its exact turn, for as long as it is claimed, started and inside
    its lease, and it names its command by ``wake_key``. Any other turn, whatever
    started it and whenever, has another origin id and reads nothing.
    """
    await scope(db, session.tenant_id)
    wake = await db.scalar(
        select(TaskContinuation).where(
            TaskContinuation.tenant_id == session.tenant_id,
            TaskContinuation.platform == session.platform,
            TaskContinuation.thread_id == session.thread_id,
            TaskContinuation.lease_owner == origin.id.hex,
            TaskContinuation.status == "claimed",
            TaskContinuation.started_at.is_not(None),
            TaskContinuation.lease_expires_at > now,
        )
    )
    place = (session.tenant_id, session.platform, session.thread_id, session.parent_channel_id)
    if (
        wake is None
        or origin.attended
        or origin.helper_of is not None
        or (origin.tenant_id, origin.platform, origin.thread_id, origin.parent_channel_id) != place
        or wake.parent_channel_id != session.parent_channel_id
        or not origin.account_id == wake.requester_account_id == session.owner_account_id
        or not origin.responder_ma_agent_id == wake.target_ma_agent_id == session.agent_id
    ):
        return False
    finished = await db.scalars(
        select(ComputeTaskRecord.id).where(
            ComputeTaskRecord.tenant_id == session.tenant_id,
            ComputeTaskRecord.session_id == session.id,
            ComputeTaskRecord.state.not_in(ACTIVE_TASK_STATES),
        )
    )
    return wake.idempotency_key in {wake_key(task_id) for task_id in finished}
