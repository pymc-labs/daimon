"""Reads behind a workspace's integrations page: GitHub use, sync trouble, sign-ins.

Nothing here writes, and no row carries a credential: the encrypted columns of the
stores they sit beside are never selected.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Literal, cast

from daimon.core._models import (
    AgentGitHubGrant,
    AgentMcpCredential,
    GitHubAppInstallation,
    GitHubInstallationReconciliation,
    GitHubPushResync,
    TenantGitHubRepo,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "GrantedRepoRow",
    "InstallationUseRow",
    "SyncTroubleRow",
    "SharedCredentialRow",
    "installations_in_use",
    "get_installation_states",
    "list_granted_repos",
    "list_shared_credentials",
    "list_sync_trouble",
]


class InstallationUseRow(BaseModel):
    """One GitHub App installation the workspace's repos rest on."""

    model_config = ConfigDict(frozen=True)

    installation_id: int
    account_login: str
    repository_selection: str | None
    suspended_at: datetime | None
    removed_at: datetime | None
    refreshed_at: datetime | None
    repos: int


class GrantedRepoRow(BaseModel):
    """One repo an agent may use, as the agent sees it (its own row over a shared one)."""

    model_config = ConfigDict(frozen=True)

    agent_id: uuid.UUID
    repo_id: int
    full_name: str
    scope: Literal["agent", "shared"]
    access: Literal["read", "write"]
    status: Literal["active", "suspended", "revoked"]
    working: bool


class SyncTroubleRow(BaseModel):
    """A queued sync that last failed: a repo's push resync or an installation refresh."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["push", "installation"]
    subject: str
    attempts: int
    error: str
    at: datetime


class SharedCredentialRow(BaseModel):
    """An agent holds a shared token for a server. The token itself is never read."""

    model_config = ConfigDict(frozen=True)

    agent_id: uuid.UUID
    server_url: str


async def installations_in_use(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[InstallationUseRow]:
    """The installations the workspace's confirmed repos name, with how many repos each backs."""
    counts = (
        await session.execute(
            select(
                TenantGitHubRepo.installation_id,
                func.count(func.distinct(TenantGitHubRepo.repo_id)),
            )
            .where(TenantGitHubRepo.tenant_id == tenant_id)
            .group_by(TenantGitHubRepo.installation_id)
        )
    ).all()
    if not counts:
        return []
    found = {
        row.installation_id: row
        for row in await session.scalars(
            select(GitHubAppInstallation).where(
                GitHubAppInstallation.installation_id.in_([c[0] for c in counts])
            )
        )
    }
    rows: list[InstallationUseRow] = []
    for installation_id, repos in counts:
        installation = found.get(installation_id)
        if installation is None:
            continue
        rows.append(
            InstallationUseRow(
                installation_id=installation_id,
                account_login=installation.account_login,
                repository_selection=installation.repository_selection,
                suspended_at=installation.suspended_at,
                removed_at=installation.github_removed_at,
                refreshed_at=installation.github_refreshed_at,
                repos=repos,
            )
        )
    return sorted(rows, key=lambda r: r.account_login.casefold())


async def get_installation_states(
    session: AsyncSession, *, installation_ids: Collection[int]
) -> dict[int, Literal["active", "suspended", "removed"]]:
    """Each known installation's state; an unknown id is left out."""
    if not installation_ids:
        return {}
    rows = await session.scalars(
        select(GitHubAppInstallation).where(
            GitHubAppInstallation.installation_id.in_(list(installation_ids))
        )
    )
    return {
        row.installation_id: "removed"
        if row.github_removed_at is not None
        else "suspended"
        if row.suspended_at is not None
        else "active"
        for row in rows
    }


async def list_granted_repos(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[GrantedRepoRow]:
    """Every live grant in the workspace, each resolved to the row its agent may use."""
    grants = list(
        await session.scalars(
            select(AgentGitHubGrant).where(
                AgentGitHubGrant.tenant_id == tenant_id,
                AgentGitHubGrant.app_visible.is_(True),
            )
        )
    )
    repos = list(
        await session.scalars(
            select(TenantGitHubRepo).where(TenantGitHubRepo.tenant_id == tenant_id)
        )
    )
    shared = {r.repo_id: r for r in repos if r.scope_agent_id is None}
    own = {(r.scope_agent_id, r.repo_id): r for r in repos if r.scope_agent_id is not None}
    rows: list[GrantedRepoRow] = []
    for grant in grants:
        row = own.get((grant.agent_id, grant.repo_id)) or shared.get(grant.repo_id)
        if row is None:
            continue
        rows.append(
            GrantedRepoRow(
                agent_id=grant.agent_id,
                repo_id=grant.repo_id,
                full_name=row.repo_full_name,
                scope="shared" if row.scope_agent_id is None else "agent",
                access=cast(Literal["read", "write"], row.max_access),
                status=cast(Literal["active", "suspended", "revoked"], row.status),
                working=grant.is_working_repo,
            )
        )
    return sorted(rows, key=lambda r: (str(r.agent_id), r.full_name.casefold()))


async def list_sync_trouble(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 50
) -> list[SyncTroubleRow]:
    """Syncs of the workspace's repos and installations that last failed, newest first."""
    repo_names = select(TenantGitHubRepo.repo_full_name).where(
        TenantGitHubRepo.tenant_id == tenant_id
    )
    pushes = await session.scalars(
        select(GitHubPushResync)
        .where(
            GitHubPushResync.repo_full_name.in_(repo_names),
            GitHubPushResync.last_error.is_not(None),
        )
        .order_by(GitHubPushResync.updated_at.desc())
        .limit(limit)
    )
    installation_ids = select(TenantGitHubRepo.installation_id).where(
        TenantGitHubRepo.tenant_id == tenant_id
    )
    refreshes = await session.execute(
        select(GitHubInstallationReconciliation, GitHubAppInstallation.account_login)
        .join(
            GitHubAppInstallation,
            GitHubAppInstallation.installation_id
            == GitHubInstallationReconciliation.installation_id,
        )
        .where(
            GitHubInstallationReconciliation.installation_id.in_(installation_ids),
            GitHubInstallationReconciliation.last_error.is_not(None),
        )
        .order_by(GitHubInstallationReconciliation.updated_at.desc())
        .limit(limit)
    )
    found = [
        SyncTroubleRow(
            kind="push",
            subject=f"{job.repo_full_name} {job.ref.removeprefix('refs/heads/')}",
            attempts=job.attempts,
            error=job.last_error or "",
            at=job.updated_at,
        )
        for job in pushes
    ] + [
        SyncTroubleRow(
            kind="installation",
            subject=login,
            attempts=job.attempts,
            error=job.last_error or "",
            at=job.updated_at,
        )
        for job, login in refreshes
    ]
    return sorted(found, key=lambda r: r.at, reverse=True)[:limit]


async def list_shared_credentials(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_ids: Collection[uuid.UUID]
) -> list[SharedCredentialRow]:
    """Which agent holds a shared token for which server: ids and URLs only."""
    if not agent_ids:
        return []
    result = await session.execute(
        select(AgentMcpCredential.agent_id, AgentMcpCredential.mcp_server_url).where(
            AgentMcpCredential.tenant_id == tenant_id,
            AgentMcpCredential.agent_id.in_(list(agent_ids)),
        )
    )
    return [SharedCredentialRow(agent_id=a, server_url=u) for a, u in result.all()]
