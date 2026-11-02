"""Durable, coalesced GitHub installation repository refresh work."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from daimon.core._models import (
    GitHubAppInstallation,
    GitHubInstallationDelivery,
    GitHubInstallationReconciliation,
)
from daimon.core.stores.domain import GitHubInstallationReconciliationRow
from daimon.core.stores.github_app_installations import lock_for_reconciliation
from daimon.core.stores.github_new_repo_notices import queue_new_repos
from daimon.core.stores.github_removal_notices import (
    cancel_unconfirmed_removal,
    confirm_removal,
    queue_removal,
)
from sqlalchemy import CursorResult, DateTime, and_, case, delete, literal, or_, select, update
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
    previous = await lock_for_reconciliation(session, installation_id=installation_id)
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
        # Every writer locks the installation before its reconciliation row.
        if previous is not None:
            await queue_removal(
                session,
                installation_id=installation_id,
                account_login=previous.account_login,
                now=now,
                confirmed=False,
            )
        await session.execute(
            delete(GitHubAppInstallation).where(
                GitHubAppInstallation.installation_id == installation_id,
                GitHubAppInstallation.app == "legacy",
            )
        )
    return True


async def enqueue_never_refreshed(session: AsyncSession, *, now: datetime, limit: int) -> int:
    """Queue a refresh for each live App installation GitHub has never been read for.

    Migration 0108 queues these once, but a scheduler still running the previous
    image can complete those jobs without writing the snapshot. This statement
    heals that: it selects only installations with no snapshot, not removed, and
    with no queue row or a ``done`` one, so a pending (including backing-off) or
    running job already counts as queued. The conflict clause re-checks ``done``
    so a job claimed between the select and the write is left alone.
    """
    installation = GitHubAppInstallation
    queue = GitHubInstallationReconciliation
    candidates = (
        select(
            installation.installation_id,
            literal(1),
            literal("pending"),
            literal(0),
            literal(now, type_=DateTime(timezone=True)),
        )
        .outerjoin(queue, queue.installation_id == installation.installation_id)
        .where(
            installation.app == "github_app",
            installation.github_refreshed_at.is_(None),
            installation.github_removed_at.is_(None),
            or_(queue.installation_id.is_(None), queue.state == "done"),
        )
        .order_by(installation.installation_id)
        .limit(limit)
    )
    stmt = (
        pg_insert(queue)
        .from_select(
            ["installation_id", "generation", "state", "attempts", "available_at"], candidates
        )
        .on_conflict_do_update(
            index_elements=["installation_id"],
            set_={
                "generation": queue.generation + 1,
                "attempts": 0,
                "state": "pending",
                "available_at": now,
                "last_error": None,
                "updated_at": now,
            },
            where=queue.state == "done",
        )
        .returning(queue.installation_id)
    )
    return len((await session.execute(stmt)).all())


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
    candidate_ids = list(
        await session.scalars(
            select(GitHubInstallationReconciliation.installation_id)
            .where(due)
            .order_by(
                GitHubInstallationReconciliation.available_at,
                GitHubInstallationReconciliation.created_at,
                GitHubInstallationReconciliation.installation_id,
            )
            .limit(50)
        )
    )
    for installation_id in candidate_ids:
        # Roll back a skipped candidate's savepoint to release its installation
        # lock before trying another ID. The worker holds one installation lock.
        async with session.begin_nested() as savepoint:
            await lock_for_reconciliation(session, installation_id=installation_id)
            row = await session.scalar(
                select(GitHubInstallationReconciliation)
                .where(GitHubInstallationReconciliation.installation_id == installation_id, due)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if row is None:
                await savepoint.rollback()
                continue
            row.state = "running"
            row.claimed_generation = row.generation
            row.lease_owner = lease_owner
            row.lease_expires_at = now + lease_for
            row.attempts += 1
            row.updated_at = now
            await session.flush()
            await session.refresh(row)
            return GitHubInstallationReconciliationRow.model_validate(row)
    return None


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
    suspended_at: datetime | None = None,
    repository_selection: Literal["all", "selected"] | None = None,
) -> bool:
    """Write a complete snapshot only for the current claim and generation.

    ``account_login is None`` means GitHub confirmed the installation no longer
    exists. ``repos`` must be a complete successfully paginated list otherwise.
    ``repository_selection`` refreshes a live App installation's stored value;
    None keeps what is stored.
    """
    previous = await lock_for_reconciliation(session, installation_id=job.installation_id)
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
        if previous is not None:
            await queue_removal(
                session,
                installation_id=job.installation_id,
                account_login=previous.account_login,
                now=now,
                confirmed=True,
            )
        await confirm_removal(session, installation_id=job.installation_id, now=now)
        if previous is not None and previous.app == "github_app":
            previous.github_removed_at = now
            previous.github_refreshed_at = now
        await session.execute(
            delete(GitHubAppInstallation).where(
                GitHubAppInstallation.installation_id == job.installation_id,
                GitHubAppInstallation.app == "legacy",
            )
        )
    else:
        await cancel_unconfirmed_removal(session, installation_id=job.installation_id)
        if previous is not None and previous.app == "github_app":
            if repos is None and suspended_at is None:
                raise ValueError("a live installation requires a complete repository snapshot")
            if repos is not None:
                previous.github_repo_full_names = repos
            previous.github_refreshed_at = now
            previous.github_removed_at = None
            previous.suspended_at = suspended_at
            if repository_selection is not None:
                previous.repository_selection = repository_selection
        else:
            if repos is None:
                raise ValueError("a live installation requires a complete repository snapshot")
        if previous is not None and previous.app == "legacy" and repos is not None:
            await queue_new_repos(
                session,
                installation_id=job.installation_id,
                old_names=set(previous.repo_full_names),
                new_names=set(repos),
                now=now,
            )
        if repos is not None:
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
                    where=GitHubAppInstallation.app == "legacy",
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
