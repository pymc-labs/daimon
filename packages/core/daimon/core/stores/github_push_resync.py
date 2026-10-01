"""Durable GitHub push resync queue and delivery receipts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from daimon.core._models import GitHubPushDelivery, GitHubPushResync
from daimon.core.stores.domain import GitHubPushResyncRow
from sqlalchemy import CursorResult, and_, case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

DELIVERY_RETENTION = timedelta(days=30)


async def enqueue(
    session: AsyncSession,
    *,
    repo_full_name: str,
    ref: str,
    delivery_id: str,
) -> bool:
    """Persist a verified delivery; return False when its receipt already exists."""
    receipt = (
        pg_insert(GitHubPushDelivery)
        .values(delivery_id=delivery_id, repo_full_name=repo_full_name, ref=ref)
        .on_conflict_do_nothing(index_elements=["delivery_id"])
        .returning(GitHubPushDelivery.delivery_id)
    )
    inserted = (await session.execute(receipt)).scalar_one_or_none()
    if inserted is None:
        return False

    now = datetime.now(UTC)
    stmt = (
        pg_insert(GitHubPushResync)
        .values(
            repo_full_name=repo_full_name,
            ref=ref,
            delivery_id=delivery_id,
            generation=1,
            state="pending",
            attempts=0,
            available_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_github_push_resyncs_repo_ref",
            set_={
                "delivery_id": delivery_id,
                "generation": GitHubPushResync.generation + 1,
                # A fresh push starts a new retry streak.
                "attempts": 0,
                "state": case((GitHubPushResync.state == "running", "running"), else_="pending"),
                "available_at": case(
                    (GitHubPushResync.state == "running", GitHubPushResync.available_at),
                    else_=now,
                ),
                "last_error": None,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)
    return True


async def get_for_repo_ref(
    session: AsyncSession,
    *,
    repo_full_name: str,
    ref: str,
) -> GitHubPushResyncRow | None:
    result = await session.execute(
        select(GitHubPushResync)
        .where(
            GitHubPushResync.repo_full_name == repo_full_name,
            GitHubPushResync.ref == ref,
        )
        .execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    return GitHubPushResyncRow.model_validate(row) if row is not None else None


async def claim_due(
    session: AsyncSession,
    *,
    lease_owner: uuid.UUID,
    lease_for: timedelta,
    now: datetime,
) -> GitHubPushResyncRow | None:
    """Claim the oldest due job, including jobs whose prior lease expired."""
    due = or_(
        and_(GitHubPushResync.state == "pending", GitHubPushResync.available_at <= now),
        and_(
            GitHubPushResync.state == "running",
            GitHubPushResync.lease_expires_at <= now,
        ),
    )
    result = await session.execute(
        select(GitHubPushResync)
        .where(due)
        .order_by(GitHubPushResync.available_at, GitHubPushResync.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    row.state = "running"
    row.claimed_generation = row.generation
    row.lease_owner = lease_owner
    row.lease_expires_at = now + lease_for
    row.attempts += 1
    row.updated_at = now
    await session.flush()
    return GitHubPushResyncRow.model_validate(row)


async def renew_lease(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    lease_owner: uuid.UUID,
    lease_for: timedelta,
    now: datetime,
) -> bool:
    result = await session.execute(
        update(GitHubPushResync)
        .where(
            GitHubPushResync.id == job_id,
            GitHubPushResync.state == "running",
            GitHubPushResync.lease_owner == lease_owner,
        )
        .values(lease_expires_at=now + lease_for, updated_at=now)
        .returning(GitHubPushResync.id)
    )
    return result.scalar_one_or_none() is not None


async def complete(
    session: AsyncSession,
    *,
    job: GitHubPushResyncRow,
    lease_owner: uuid.UUID,
    now: datetime,
) -> bool:
    """Complete only this owner's claim; newer pushes remain pending."""
    same_generation = GitHubPushResync.generation == job.claimed_generation
    result = await session.execute(
        update(GitHubPushResync)
        .where(
            GitHubPushResync.id == job.id,
            GitHubPushResync.state == "running",
            GitHubPushResync.lease_owner == lease_owner,
            GitHubPushResync.claimed_generation == job.claimed_generation,
        )
        .values(
            state=case((same_generation, "done"), else_="pending"),
            available_at=case((same_generation, GitHubPushResync.available_at), else_=now),
            claimed_generation=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error=case((same_generation, None), else_=GitHubPushResync.last_error),
            updated_at=now,
        )
        .returning(GitHubPushResync.id)
    )
    return result.scalar_one_or_none() is not None


async def retry(
    session: AsyncSession,
    *,
    job: GitHubPushResyncRow,
    lease_owner: uuid.UUID,
    retry_after: datetime,
    error: str,
    now: datetime,
) -> bool:
    """Release a failed claim with backoff; newer generations are immediately due."""
    same_generation = GitHubPushResync.generation == job.claimed_generation
    result = await session.execute(
        update(GitHubPushResync)
        .where(
            GitHubPushResync.id == job.id,
            GitHubPushResync.state == "running",
            GitHubPushResync.lease_owner == lease_owner,
            GitHubPushResync.claimed_generation == job.claimed_generation,
        )
        .values(
            state="pending",
            available_at=case((same_generation, retry_after), else_=now),
            claimed_generation=None,
            lease_owner=None,
            lease_expires_at=None,
            last_error=case((same_generation, error[:2000]), else_=None),
            updated_at=now,
        )
        .returning(GitHubPushResync.id)
    )
    return result.scalar_one_or_none() is not None


async def request_current_generation_pass(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Schedule convergence after a stale worker reports a late external result."""
    result = await session.execute(
        update(GitHubPushResync)
        .where(GitHubPushResync.id == job_id)
        .values(
            generation=GitHubPushResync.generation + 1,
            state=case((GitHubPushResync.state == "running", "running"), else_="pending"),
            available_at=case(
                (GitHubPushResync.state == "running", GitHubPushResync.available_at),
                else_=now,
            ),
            last_error=None,
            updated_at=now,
        )
        .returning(GitHubPushResync.id)
    )
    return result.scalar_one_or_none() is not None


async def sweep_delivery_receipts(session: AsyncSession, *, now: datetime) -> int:
    """Delete delivery receipts outside the finite idempotency window."""
    result = await session.execute(
        delete(GitHubPushDelivery).where(GitHubPushDelivery.received_at < now - DELIVERY_RETENTION)
    )
    return cast(CursorResult[Any], result).rowcount or 0
