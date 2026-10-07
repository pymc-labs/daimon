"""Queue new installation repos for a later admin-facing notice card."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import cast

from daimon.core._models import GitHubNewRepoNotice, TenantGitHubRepo
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


class NewRepoNotice(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    installation_id: int
    repo_full_name: str
    claimed_at: datetime


async def claim_next_notice(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> NewRepoNotice | None:
    """Lease one undelivered card. A failed post can release it immediately."""
    row = await session.scalar(
        select(GitHubNewRepoNotice)
        .where(
            GitHubNewRepoNotice.tenant_id == tenant_id,
            GitHubNewRepoNotice.delivered_at.is_(None),
            GitHubNewRepoNotice.dismissed_at.is_(None),
            (
                GitHubNewRepoNotice.claimed_at.is_(None)
                | (GitHubNewRepoNotice.claimed_at < now - timedelta(minutes=10))
            ),
        )
        .order_by(GitHubNewRepoNotice.queued_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.claimed_at = now
    await session.flush()
    return NewRepoNotice.model_validate(row)


async def finish_notice(
    session: AsyncSession, *, notice: NewRepoNotice, delivered: bool, now: datetime
) -> bool:
    row = await session.get(
        GitHubNewRepoNotice,
        (notice.tenant_id, notice.installation_id, notice.repo_full_name),
        with_for_update=True,
    )
    if row is None or row.claimed_at != notice.claimed_at:
        return False
    row.claimed_at = None
    if delivered:
        row.delivered_at = now
    await session.flush()
    return True


async def dismiss_notice(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    installation_id: int,
    repo_full_name: str,
    now: datetime,
) -> bool:
    row = await session.get(
        GitHubNewRepoNotice,
        (tenant_id, installation_id, repo_full_name),
        with_for_update=True,
    )
    if row is None or row.dismissed_at is not None:
        return False
    row.dismissed_at = now
    await session.flush()
    return True


async def queue_new_repos(
    session: AsyncSession,
    *,
    installation_id: int,
    old_names: set[str],
    new_names: set[str],
    now: datetime,
) -> int:
    """Queue once per tenant already connected to this installation."""
    added = new_names - old_names
    if not added:
        return 0
    tenants = set(
        await session.scalars(
            select(TenantGitHubRepo.tenant_id).where(
                TenantGitHubRepo.installation_id == installation_id,
                TenantGitHubRepo.status == "active",
            )
        )
    )
    count = 0
    for tenant_id in tenants:
        for full_name in added:
            result = await session.execute(
                pg_insert(GitHubNewRepoNotice)
                .values(
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    repo_full_name=full_name,
                    queued_at=now,
                )
                .on_conflict_do_nothing()
            )
            count += cast(CursorResult[object], result).rowcount or 0
    return count
