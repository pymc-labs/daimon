"""Queued first turns for an agent a task was just handed to.

Handing a task over may carry work to continue. That continuation is a turn
somebody will be billed for, posted into a thread people are reading, so it
must happen at most once — across process restarts, across two adapter
processes, across a retry loop. `claim_continuation` is the whole guarantee: a
single conditional UPDATE, so the database decides the winner and everyone else
gets False and does nothing.

`requested_work is None` means the handoff carried no work: the switch is
recorded for the audit trail and nothing is ever dispatched.

The `*_wake_*` functions are the leased form of the same ladder, used by the
wake queue (`daimon.core.continuity.wakes`): a claim names an owner and an
expiry, `started_at` fences the turn, and every later write is guarded on the
owner so a process that lost its lease can change nothing.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta
from typing import Literal

from daimon.core._models import TaskContinuation
from daimon.core.stores.domain import ContinuationReason, TaskContinuationRow
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement


async def record_continuation(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    requester_account_id: _uuid.UUID,
    requester_external_user_id: str,
    target_ma_agent_id: str,
    target_name: str,
    reason: ContinuationReason,
    idempotency_key: _uuid.UUID,
    requested_work: str | None = None,
    available_at: datetime | None = None,
) -> TaskContinuationRow:
    """Queue a continuation as `pending`. Writing it dispatches nothing.

    `target_ma_agent_id` is stored concrete, never a name: a name resolves
    differently later, and a continuation must reach the agent the requester
    actually chose. `idempotency_key` is minted by the caller so a retried tool
    call recognises its own row instead of queueing a second one.
    `available_at` makes it a wake: polled, and not claimable before then.
    """
    orm = TaskContinuation(
        tenant_id=tenant_id,
        platform=platform,
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
        requester_account_id=requester_account_id,
        requester_external_user_id=requester_external_user_id,
        target_ma_agent_id=target_ma_agent_id,
        target_name=target_name,
        requested_work=requested_work,
        reason=reason,
        idempotency_key=idempotency_key,
        available_at=available_at,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return TaskContinuationRow.model_validate(orm)


async def claim_continuation(
    session: AsyncSession,
    *,
    idempotency_key: _uuid.UUID,
    now: datetime,
) -> bool:
    """Take exclusive responsibility for dispatching this continuation.

    The at-most-once primitive. One `UPDATE … WHERE status = 'pending'
    RETURNING`: Postgres serializes two racing transactions on the row lock,
    and the loser re-evaluates the predicate against the winner's committed
    `'claimed'` and matches nothing. Exactly one caller sees True, so exactly
    one turn is ever dispatched — a restart mid-dispatch cannot double-post.

    A True return means the caller now owns the row and must settle it. A
    wake that is not due yet (`available_at` in the future) is never claimable.
    """
    claimed = (
        await session.execute(
            update(TaskContinuation)
            .where(
                TaskContinuation.idempotency_key == idempotency_key,
                TaskContinuation.status == "pending",
                or_(TaskContinuation.available_at.is_(None), TaskContinuation.available_at <= now),
            )
            .values(status="claimed", claimed_at=now)
            .returning(TaskContinuation.id)
        )
    ).scalar_one_or_none()
    await session.flush()
    return claimed is not None


async def settle_continuation(
    session: AsyncSession,
    *,
    idempotency_key: _uuid.UUID,
    status: Literal["delivered", "skipped"],
    now: datetime,
    skip_reason: str | None = None,
) -> None:
    """Close out a claimed continuation, delivered or deliberately skipped.

    `delivered_at` is stamped only on delivery, so a skipped row never reads as
    though a turn ran for it.
    """
    values: dict[str, object] = {"status": status, "skip_reason": skip_reason}
    if status == "delivered":
        values["delivered_at"] = now
    await session.execute(
        update(TaskContinuation)
        .where(TaskContinuation.idempotency_key == idempotency_key)
        .values(**values)
    )
    await session.flush()


async def get_continuation(
    session: AsyncSession,
    *,
    idempotency_key: _uuid.UUID,
) -> TaskContinuationRow | None:
    orm = (
        await session.execute(
            select(TaskContinuation).where(TaskContinuation.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    return None if orm is None else TaskContinuationRow.model_validate(orm)


async def list_pending_continuations(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    thread_id: str,
) -> list[TaskContinuationRow]:
    """Undispatched continuations for one thread that are due now, oldest first.

    A wake scheduled for later (`available_at` in the future, by the database
    clock) is left out, so a caller that predates the wake queue can never
    run it early.
    """
    rows = (
        await session.execute(
            select(TaskContinuation)
            .where(
                TaskContinuation.tenant_id == tenant_id,
                TaskContinuation.platform == platform,
                TaskContinuation.thread_id == thread_id,
                TaskContinuation.status == "pending",
                or_(
                    TaskContinuation.available_at.is_(None),
                    TaskContinuation.available_at <= func.now(),
                ),
            )
            .order_by(TaskContinuation.created_at, TaskContinuation.id)
        )
    ).scalars()
    return [TaskContinuationRow.model_validate(row) for row in rows]


def _dispatchable(now: datetime, *, max_attempts: int) -> ColumnElement[bool]:
    """Rows a dispatcher may claim at `now`.

    Pending and due, or claimed by a process whose lease ran out before it
    committed the start fence — nothing can have run for such a row, so taking
    it over cannot repeat a turn. A claim with `started_at` set is never
    dispatchable again, however stale.
    """
    return or_(
        and_(
            TaskContinuation.status == "pending",
            or_(TaskContinuation.available_at.is_(None), TaskContinuation.available_at <= now),
        ),
        and_(
            TaskContinuation.status == "claimed",
            TaskContinuation.lease_expires_at.is_not(None),
            TaskContinuation.lease_expires_at < now,
            TaskContinuation.started_at.is_(None),
            TaskContinuation.attempts < max_attempts,
        ),
    )


async def claim_wake_row(
    session: AsyncSession,
    *,
    idempotency_key: _uuid.UUID,
    owner: str,
    now: datetime,
    lease: timedelta,
    max_attempts: int,
) -> TaskContinuationRow | None:
    """Claim one row under a lease; the claimed row, or None if it was not claimable.

    Same single conditional UPDATE as `claim_continuation`, so two racing
    dispatchers still get exactly one winner, widened to take over an expired,
    unstarted claim.
    """
    orm = (
        await session.execute(
            update(TaskContinuation)
            .where(
                TaskContinuation.idempotency_key == idempotency_key,
                _dispatchable(now, max_attempts=max_attempts),
            )
            .values(
                status="claimed",
                claimed_at=now,
                lease_owner=owner,
                lease_expires_at=now + lease,
                attempts=TaskContinuation.attempts + 1,
            )
            .returning(TaskContinuation)
        )
    ).scalar_one_or_none()
    await session.flush()
    return None if orm is None else TaskContinuationRow.model_validate(orm)


async def start_wake_row(
    session: AsyncSession,
    *,
    idempotency_key: _uuid.UUID,
    owner: str,
    now: datetime,
    lease: timedelta,
) -> bool:
    """Commit the start fence; False when `owner` no longer holds the claim.

    Not gated on the lease still being live: a takeover is itself an UPDATE on
    this row, so either it landed first (the owner no longer matches) or it
    comes after the fence and finds `started_at` set.
    """
    started = (
        await session.execute(
            update(TaskContinuation)
            .where(
                TaskContinuation.idempotency_key == idempotency_key,
                TaskContinuation.status == "claimed",
                TaskContinuation.lease_owner == owner,
                TaskContinuation.started_at.is_(None),
            )
            .values(started_at=now, lease_expires_at=now + lease)
            .returning(TaskContinuation.id)
        )
    ).scalar_one_or_none()
    await session.flush()
    return started is not None


async def settle_wake_row(
    session: AsyncSession,
    *,
    idempotency_key: _uuid.UUID,
    owner: str,
    status: Literal["delivered", "skipped"],
    now: datetime,
    skip_reason: str | None = None,
) -> bool:
    """Close out a claim this owner still holds; False when it was taken over."""
    values: dict[str, object] = {
        "status": status,
        "skip_reason": skip_reason,
        "lease_expires_at": None,
    }
    if status == "delivered":
        values["delivered_at"] = now
    settled = (
        await session.execute(
            update(TaskContinuation)
            .where(
                TaskContinuation.idempotency_key == idempotency_key,
                TaskContinuation.status == "claimed",
                TaskContinuation.lease_owner == owner,
            )
            .values(**values)
            .returning(TaskContinuation.id)
        )
    ).scalar_one_or_none()
    await session.flush()
    return settled is not None


async def release_wake_row(
    session: AsyncSession,
    *,
    idempotency_key: _uuid.UUID,
    owner: str,
    retry_at: datetime | None,
) -> bool:
    """Hand a claim back because its turn could not run yet; False if the claim was lost.

    Only the owner may release, and only when it knows the turn did not run —
    which is why this clears `started_at` where a takeover never could. A
    release is a deferral, not a failed attempt: the claim it took is refunded,
    so a thread that stays busy for a long time never uses up the budget that
    is kept for crashes. `retry_at=None` keeps the row's `available_at` as it
    is, so a handoff (NULL) waits for the next turn in its thread exactly as it
    always did; a wake passes the instant to retry at.
    """
    values: dict[str, object] = {
        "status": "pending",
        "lease_owner": None,
        "lease_expires_at": None,
        "started_at": None,
        "attempts": TaskContinuation.attempts - 1,
    }
    if retry_at is not None:
        values["available_at"] = retry_at
    released = (
        await session.execute(
            update(TaskContinuation)
            .where(
                TaskContinuation.idempotency_key == idempotency_key,
                TaskContinuation.status == "claimed",
                TaskContinuation.lease_owner == owner,
            )
            .values(**values)
            .returning(TaskContinuation.id)
        )
    ).scalar_one_or_none()
    await session.flush()
    return released is not None


async def cancel_wake_row(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    idempotency_key: _uuid.UUID,
) -> bool:
    """Withdraw a wake nobody has claimed yet; False once it is claimed or settled.

    A status flip, never a delete: a claim is a conditional UPDATE on
    `status = 'pending'`, so a cancelled row can never be claimed afterwards.
    """
    cancelled = (
        await session.execute(
            update(TaskContinuation)
            .where(
                TaskContinuation.tenant_id == tenant_id,
                TaskContinuation.idempotency_key == idempotency_key,
                TaskContinuation.status == "pending",
            )
            .values(status="cancelled", skip_reason="cancelled")
            .returning(TaskContinuation.id)
        )
    ).scalar_one_or_none()
    await session.flush()
    return cancelled is not None


async def list_dispatchable_continuations(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    thread_id: str,
    now: datetime,
    max_attempts: int,
) -> list[TaskContinuationRow]:
    """Rows in one thread a dispatcher may claim now, oldest first."""
    rows = (
        await session.execute(
            select(TaskContinuation)
            .where(
                TaskContinuation.tenant_id == tenant_id,
                TaskContinuation.platform == platform,
                TaskContinuation.thread_id == thread_id,
                _dispatchable(now, max_attempts=max_attempts),
            )
            .order_by(TaskContinuation.created_at, TaskContinuation.id)
        )
    ).scalars()
    return [TaskContinuationRow.model_validate(row) for row in rows]


async def list_due_wake_rows(
    session: AsyncSession,
    *,
    platform: str,
    now: datetime,
    max_attempts: int,
    limit: int,
) -> list[TaskContinuationRow]:
    """The oldest actionable row of up to `limit` threads, longest-waiting thread first.

    Actionable: a due wake, or a claim to take over. A pending row with no
    `available_at` is left to the next turn in its thread, as it always was;
    an expired unstarted claim is taken over whatever its origin, since that
    is the only way it would ever settle. One row per thread, so a thread with
    many rows cannot crowd the others out of the batch; a thread the adapter
    could not open is pushed back (`defer_wake_rows`), so the scan moves on.
    """
    actionable = and_(
        TaskContinuation.platform == platform,
        _dispatchable(now, max_attempts=max_attempts),
        or_(
            TaskContinuation.status == "claimed",
            TaskContinuation.available_at.is_not(None),
        ),
    )
    first_per_thread = (
        select(TaskContinuation.id)
        .where(actionable)
        .distinct(TaskContinuation.tenant_id, TaskContinuation.thread_id)
        .order_by(
            TaskContinuation.tenant_id,
            TaskContinuation.thread_id,
            TaskContinuation.available_at.nulls_first(),
            TaskContinuation.id,
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(TaskContinuation)
            .where(TaskContinuation.id.in_(select(first_per_thread.c.id)))
            .order_by(TaskContinuation.available_at.nulls_first(), TaskContinuation.id)
            .limit(limit)
        )
    ).scalars()
    return [TaskContinuationRow.model_validate(row) for row in rows]


async def defer_wake_rows(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    thread_id: str,
    now: datetime,
    retry_at: datetime,
    max_attempts: int,
) -> int:
    """Push a thread's actionable rows back to `retry_at`; how many moved.

    For a thread the adapter could not open this poll (no token, workspace
    archived, platform error). Nothing ran, so an expired unstarted claim goes
    back to pending too, without spending an attempt. Rows are not skipped:
    the thread may come back.
    """
    moved = (
        await session.execute(
            update(TaskContinuation)
            .where(
                TaskContinuation.tenant_id == tenant_id,
                TaskContinuation.platform == platform,
                TaskContinuation.thread_id == thread_id,
                _dispatchable(now, max_attempts=max_attempts),
                or_(
                    TaskContinuation.status == "claimed",
                    TaskContinuation.available_at.is_not(None),
                ),
            )
            .values(
                status="pending",
                available_at=retry_at,
                lease_owner=None,
                lease_expires_at=None,
            )
            .returning(TaskContinuation.id)
        )
    ).scalars()
    count = len(list(moved))
    await session.flush()
    return count


async def abandon_interrupted_wake_rows(
    session: AsyncSession,
    *,
    platform: str,
    now: datetime,
    max_attempts: int,
) -> list[TaskContinuationRow]:
    """Settle expired claims that must not be run again, and return them.

    `started_at` set means the turn may already have run and been seen, so it
    settles `skipped/interrupted`; running it again is the duplicate the
    at-most-once rule forbids. An unstarted claim out of attempts settles
    `skipped/attempts_exhausted`.
    """
    expired = and_(
        TaskContinuation.platform == platform,
        TaskContinuation.status == "claimed",
        TaskContinuation.lease_expires_at.is_not(None),
        TaskContinuation.lease_expires_at < now,
    )
    settled: list[TaskContinuationRow] = []
    for condition, reason in (
        (TaskContinuation.started_at.is_not(None), "interrupted"),
        (TaskContinuation.attempts >= max_attempts, "attempts_exhausted"),
    ):
        rows = (
            await session.execute(
                update(TaskContinuation)
                .where(expired, condition)
                .values(status="skipped", skip_reason=reason, lease_expires_at=None)
                .returning(TaskContinuation)
            )
        ).scalars()
        settled.extend(TaskContinuationRow.model_validate(row) for row in rows)
    await session.flush()
    return settled
