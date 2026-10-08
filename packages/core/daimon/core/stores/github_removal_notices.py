"""Queue and settle private admin notices for GitHub-side removal."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from daimon.core._models import (
    GitHubAccessRequest,
    GitHubRemovalNotice,
    Tenant,
    TenantGitHubRepo,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


class RemovalNotice(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    installation_id: int
    account_login: str
    claimed_at: datetime


async def queue_removal(
    session: AsyncSession,
    *,
    installation_id: int,
    account_login: str,
    now: datetime,
    confirmed: bool = False,
) -> None:
    connected = list(
        await session.scalars(
            select(TenantGitHubRepo).where(
                TenantGitHubRepo.installation_id == installation_id,
                TenantGitHubRepo.status == "active",
            )
        )
    )
    for tenant_id in {repo.tenant_id for repo in connected}:
        await session.execute(
            pg_insert(GitHubRemovalNotice)
            .values(
                tenant_id=tenant_id,
                installation_id=installation_id,
                account_login=account_login,
                queued_at=now,
                confirmed_at=now if confirmed else None,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "installation_id"],
                set_={"confirmed_at": now} if confirmed else {"account_login": account_login},
            )
        )


async def confirm_removal(session: AsyncSession, *, installation_id: int, now: datetime) -> None:
    await session.execute(
        update(GitHubRemovalNotice)
        .where(GitHubRemovalNotice.installation_id == installation_id)
        .values(confirmed_at=now)
    )
    connected = list(
        await session.scalars(
            select(TenantGitHubRepo).where(
                TenantGitHubRepo.installation_id == installation_id,
                TenantGitHubRepo.status == "active",
            )
        )
    )
    by_tenant: dict[uuid.UUID, set[str]] = {}
    for repo in connected:
        by_tenant.setdefault(repo.tenant_id, set()).add(repo.repo_full_name.casefold())
    for tenant_id, names in by_tenant.items():
        waiting = await session.scalars(
            select(GitHubAccessRequest).where(
                GitHubAccessRequest.tenant_id == tenant_id,
                GitHubAccessRequest.status.in_(("open", "waiting_github")),
            )
        )
        for request in waiting:
            if any(name.casefold() in names for name in request.repo_names):
                request.status = "cancelled"
                request.updated_at = now


async def cancel_unconfirmed_removal(session: AsyncSession, *, installation_id: int) -> None:
    await session.execute(
        delete(GitHubRemovalNotice).where(
            GitHubRemovalNotice.installation_id == installation_id,
            GitHubRemovalNotice.confirmed_at.is_(None),
        )
    )


async def pending_tenants(session: AsyncSession, *, platform: str) -> tuple[uuid.UUID, ...]:
    rows = await session.scalars(
        select(GitHubRemovalNotice.tenant_id)
        .join(Tenant, Tenant.id == GitHubRemovalNotice.tenant_id)
        .where(
            Tenant.platform == platform,
            Tenant.archived_at.is_(None),
            GitHubRemovalNotice.delivered_at.is_(None),
            GitHubRemovalNotice.confirmed_at.is_not(None),
        )
        .distinct()
        .limit(50)
    )
    return tuple(rows)


async def claim_notice(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> RemovalNotice | None:
    row = await session.scalar(
        select(GitHubRemovalNotice)
        .where(
            GitHubRemovalNotice.tenant_id == tenant_id,
            GitHubRemovalNotice.delivered_at.is_(None),
            GitHubRemovalNotice.confirmed_at.is_not(None),
            (
                GitHubRemovalNotice.claimed_at.is_(None)
                | (GitHubRemovalNotice.claimed_at < now - timedelta(minutes=10))
            ),
        )
        .order_by(GitHubRemovalNotice.queued_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    row.claimed_at = now
    await session.flush()
    return RemovalNotice.model_validate(row)


async def finish_notice(
    session: AsyncSession, *, notice: RemovalNotice, delivered: bool, now: datetime
) -> bool:
    row = await session.get(
        GitHubRemovalNotice,
        (notice.tenant_id, notice.installation_id),
        with_for_update=True,
    )
    if row is None or row.claimed_at != notice.claimed_at:
        return False
    row.claimed_at = None
    if delivered:
        row.delivered_at = now
    await session.flush()
    return True
