"""Durable, coalesced GitHub installation repository refresh work."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, cast

from daimon.core._models import (
    GitHubAppInstallation,
    GitHubInstallationDelivery,
    GitHubInstallationReconciliation,
)
from daimon.core.stores.domain import GitHubInstallationReconciliationRow
from sqlalchemy import CursorResult, and_, case, delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

DELIVERY_RETENTION = timedelta(days=30)


async def enqueue(
    session: AsyncSession,
    *,
    installation_id: int,
    delivery_id: str,
    event: str,
    deleted: bool,
    now: datetime,
) -> bool:
    """Record one delivery and coalesce it into the installation's refresh job."""
    receipt = (
        pg_insert(GitHubInstallationDelivery)
        .values(
            delivery_id=delivery_id,
            installation_id=installation_id,
            event=event,
            received_at=now,
        )
        .on_conflict_do_nothing(index_elements=["delivery_id"])
        .returning(GitHubInstallationDelivery.delivery_id)
    )
    inserted = (await session.execute(receipt)).scalar_one_or_none()
    if inserted is None:
        return False

    stmt = (
        pg_insert(GitHubInstallationReconciliation)
        .values(
            installation_id=installation_id,
            generation=1,
            state="pending",
            attempts=0,
            available_at=now,
        )
        .on_conflict_do_update(
            index_elements=["installation_id"],
            set_={
                "generation": GitHubInstallationReconciliation.generation + 1,
                "attempts": 0,
                "state": case(
                    (GitHubInstallationReconciliation.state == "running", "running"),
                    else_="pending",
                ),
                "available_at": case(
                    (
                        GitHubInstallationReconciliation.state == "running",
                        GitHubInstallationReconciliation.available_at,
                    ),
                    else_=now,
                ),
                "last_error": None,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)
    if deleted:
        # Lock the queue row before the cache row, matching finish()'s lock
        # order so a deletion cannot deadlock an in-flight snapshot write.
        await session.execute(
            delete(GitHubAppInstallation).where(
                GitHubAppInstallation.installation_id == installation_id
            )
        )
    return True


async def get(
    session: AsyncSession, *, installation_id: int
) -> GitHubInstallationReconciliationRow | None:
    result = await session.execute(
        select(GitHubInstallationReconciliation)
        .where(GitHubInstallationReconciliation.installation_id == installation_id)
        .execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    return GitHubInstallationReconciliationRow.model_validate(row) if row is not None else None


async def claim_due(
    session: AsyncSession,
    *,
    lease_owner: uuid.UUID,
    lease_for: timedelta,
    now: datetime,
) -> GitHubInstallationReconciliationRow | None:
    due = or_(
        and_(
            GitHubInstallationReconciliation.state == "pending",
            GitHubInstallationReconciliation.available_at <= now,
        ),
        and_(
            GitHubInstallationReconciliation.state == "running",
            GitHubInstallationReconciliation.lease_expires_at <= now,
        ),
    )
    result = await session.execute(
        select(GitHubInstallationReconciliation)
        .where(due)
        .order_by(
            GitHubInstallationReconciliation.available_at,
            GitHubInstallationReconciliation.created_at,
        )
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
    return GitHubInstallationReconciliationRow.model_validate(row)


async def renew_lease(
    session: AsyncSession,
    *,
    installation_id: int,
    lease_owner: uuid.UUID,
    lease_for: timedelta,
    now: datetime,
) -> bool:
    result = await session.execute(
        update(GitHubInstallationReconciliation)
        .where(
            GitHubInstallationReconciliation.installation_id == installation_id,
            GitHubInstallationReconciliation.state == "running",
            GitHubInstallationReconciliation.lease_owner == lease_owner,
        )
        .values(lease_expires_at=now + lease_for, updated_at=now)
        .returning(GitHubInstallationReconciliation.installation_id)
    )
    return result.scalar_one_or_none() is not None


async def finish(
    session: AsyncSession,
    *,
    job: GitHubInstallationReconciliationRow,
    lease_owner: uuid.UUID,
    account_login: str | None,
    repos: list[str] | None,
    now: datetime,
) -> bool:
    """Write a complete snapshot only for the current claim and generation.

    ``account_login is None`` means GitHub confirmed the installation no longer
    exists. ``repos`` must be a complete successfully paginated list otherwise.
    """
    result = await session.execute(
        select(GitHubInstallationReconciliation)
        .where(
            GitHubInstallationReconciliation.installation_id == job.installation_id,
            GitHubInstallationReconciliation.state == "running",
            GitHubInstallationReconciliation.lease_owner == lease_owner,
            GitHubInstallationReconciliation.claimed_generation == job.claimed_generation,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return False

    if row.generation != job.claimed_generation:
        row.state = "pending"
        row.available_at = now
        row.claimed_generation = None
        row.lease_owner = None
        row.lease_expires_at = None
        row.last_error = None
        row.updated_at = now
        await session.flush()
        return False

    if account_login is None:
        await session.execute(
            delete(GitHubAppInstallation).where(
                GitHubAppInstallation.installation_id == job.installation_id
            )
        )
    else:
        if repos is None:
            raise ValueError("a live installation requires a complete repository snapshot")
        stmt = (
            pg_insert(GitHubAppInstallation)
            .values(
                installation_id=job.installation_id,
                account_login=account_login,
                repo_full_names=repos,
            )
            .on_conflict_do_update(
                index_elements=["installation_id"],
                set_={
                    "account_login": account_login,
                    "repo_full_names": repos,
                    "updated_at": now,
                },
            )
        )
        await session.execute(stmt)

    row.state = "done"
    row.claimed_generation = None
    row.lease_owner = None
    row.lease_expires_at = None
    row.last_error = None
    row.updated_at = now
    await session.flush()
    return True


async def retry(
    session: AsyncSession,
    *,
    job: GitHubInstallationReconciliationRow,
    lease_owner: uuid.UUID,
    retry_after: datetime,
    error: str,
    now: datetime,
) -> bool:
    same_generation = GitHubInstallationReconciliation.generation == job.claimed_generation
    result = await session.execute(
        update(GitHubInstallationReconciliation)
        .where(
            GitHubInstallationReconciliation.installation_id == job.installation_id,
            GitHubInstallationReconciliation.state == "running",
            GitHubInstallationReconciliation.lease_owner == lease_owner,
            GitHubInstallationReconciliation.claimed_generation == job.claimed_generation,
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
        .returning(GitHubInstallationReconciliation.installation_id)
    )
    return result.scalar_one_or_none() is not None


async def sweep_delivery_receipts(session: AsyncSession, *, now: datetime) -> int:
    result = await session.execute(
        delete(GitHubInstallationDelivery).where(
            GitHubInstallationDelivery.received_at < now - DELIVERY_RETENTION
        )
    )
    return cast(CursorResult[Any], result).rowcount or 0
