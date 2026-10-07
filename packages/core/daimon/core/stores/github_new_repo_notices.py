"""Queue new installation repos for a later admin-facing notice card."""

from __future__ import annotations

from datetime import datetime
from typing import cast

from daimon.core._models import GitHubNewRepoNotice, TenantGitHubRepo
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


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
