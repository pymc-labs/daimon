"""Queue new installation repos for a later admin-facing notice card."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import cast

from daimon.core._models import GitHubNewRepoNotice, Tenant, TenantGitHubRepo
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
    queued_at: datetime
    claimed_at: datetime


class NewRepoNoticeGroup(BaseModel):
    model_config = ConfigDict(frozen=True)
    notices: tuple[NewRepoNotice, ...]

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.notices[0].tenant_id

    @property
    def repo_names(self) -> tuple[str, ...]:
        return tuple(notice.repo_full_name for notice in self.notices)


async def pending_notice_tenants(session: AsyncSession, *, platform: str) -> tuple[uuid.UUID, ...]:
    today = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    rows = await session.scalars(
        select(GitHubNewRepoNotice.tenant_id)
        .join(Tenant, Tenant.id == GitHubNewRepoNotice.tenant_id)
        .where(
            Tenant.platform == platform,
            Tenant.archived_at.is_(None),
            GitHubNewRepoNotice.delivered_at.is_(None),
            GitHubNewRepoNotice.dismissed_at.is_(None),
            GitHubNewRepoNotice.queued_at < today,
        )
        .distinct()
        .limit(50)
    )
    return tuple(rows)


async def notices_for_day(
    session: AsyncSession, *, tenant_id: uuid.UUID, day: str
) -> NewRepoNoticeGroup | None:
    try:
        date = datetime.strptime(day, "%Y%m%d").date()
    except ValueError:
        return None
    start = datetime.combine(date, time.min, tzinfo=UTC)
    rows = await session.scalars(
        select(GitHubNewRepoNotice)
        .where(
            GitHubNewRepoNotice.tenant_id == tenant_id,
            GitHubNewRepoNotice.queued_at >= start,
            GitHubNewRepoNotice.queued_at < start + timedelta(days=1),
        )
        .order_by(GitHubNewRepoNotice.repo_full_name)
    )
    notices = tuple(NewRepoNotice.model_validate(row) for row in rows)
    return NewRepoNoticeGroup(notices=notices) if notices else None


async def prior_connection_installations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    github_user_id: int,
) -> set[int]:
    """Installations this linked GitHub user previously connected here."""
    return set(
        await session.scalars(
            select(TenantGitHubRepo.installation_id).where(
                TenantGitHubRepo.tenant_id == tenant_id,
                TenantGitHubRepo.authorized_by_account_id == account_id,
                TenantGitHubRepo.authorized_by_github_user_id == github_user_id,
                TenantGitHubRepo.status == "active",
            )
        )
    )


async def claim_next_notice(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> NewRepoNotice | None:
    """Lease one undelivered card. A failed post can release it immediately."""
    today = datetime.combine(now.date(), time.min, tzinfo=UTC)
    row = await session.scalar(
        select(GitHubNewRepoNotice)
        .where(
            GitHubNewRepoNotice.tenant_id == tenant_id,
            GitHubNewRepoNotice.delivered_at.is_(None),
            GitHubNewRepoNotice.dismissed_at.is_(None),
            GitHubNewRepoNotice.queued_at < today,
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


async def claim_notice_group(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> NewRepoNoticeGroup | None:
    """Lease all of yesterday's (or an earlier day's) notices for one card."""
    first = await claim_next_notice(session, tenant_id=tenant_id, now=now)
    if first is None:
        return None
    day_start = datetime.combine(first.queued_at.date(), time.min, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    rows = await session.scalars(
        select(GitHubNewRepoNotice)
        .where(
            GitHubNewRepoNotice.tenant_id == tenant_id,
            GitHubNewRepoNotice.queued_at >= day_start,
            GitHubNewRepoNotice.queued_at < day_end,
            GitHubNewRepoNotice.delivered_at.is_(None),
            GitHubNewRepoNotice.dismissed_at.is_(None),
            (
                GitHubNewRepoNotice.claimed_at.is_(None)
                | (GitHubNewRepoNotice.claimed_at < now - timedelta(minutes=10))
            ),
        )
        .order_by(GitHubNewRepoNotice.queued_at, GitHubNewRepoNotice.repo_full_name)
        .with_for_update(skip_locked=True)
    )
    group = [first]
    for row in rows:
        row.claimed_at = now
        group.append(NewRepoNotice.model_validate(row))
    await session.flush()
    return NewRepoNoticeGroup(notices=tuple(group))


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
