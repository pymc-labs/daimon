"""Tenant-scoped GitHub authorization records. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from daimon.core._models import (
    AgentGitHubGrant,
    AgentGitHubMode,
    GitHubAppInstallation,
    TenantGitHubRepo,
)
from daimon.core.stores.security_audit import append_event
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class AuthorizedRepo(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    repo_id: int
    owner_id: int
    installation_id: int
    repo_full_name: str
    max_access: Literal["read", "write"]
    authorized_by_github_user_id: int
    authorized_by_account_id: uuid.UUID | None
    authorized_at: datetime
    status: Literal["active", "suspended", "revoked"]
    status_reason: str | None
    version: int


class AgentGrant(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    repo_id: int
    baseline_access: Literal["none", "read", "write"]
    ceiling_access: Literal["read", "write"]
    staged: bool
    mount_path: str | None
    is_working_repo: bool
    granted_by_account_id: uuid.UUID | None
    granted_at: datetime
    version: int


async def list_authorized_repos(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[AuthorizedRepo]:
    rows = await session.scalars(
        select(TenantGitHubRepo).where(TenantGitHubRepo.tenant_id == tenant_id)
    )
    return [AuthorizedRepo.model_validate(row) for row in rows]


async def list_agent_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[AgentGrant]:
    rows = await session.scalars(
        select(AgentGitHubGrant).where(
            AgentGitHubGrant.tenant_id == tenant_id, AgentGitHubGrant.agent_id == agent_id
        )
    )
    return [AgentGrant.model_validate(row) for row in rows]


async def list_live_grant_repositories(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[tuple[AgentGrant, AuthorizedRepo]]:
    """Only live grants with still-active tenant authorization and installation."""
    grants = await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    result: list[tuple[AgentGrant, AuthorizedRepo]] = []
    for grant in grants:
        if grant.staged:
            continue
        row = await session.get(TenantGitHubRepo, (tenant_id, grant.repo_id))
        if row is None or row.status != "active":
            continue
        installation = await session.get(GitHubAppInstallation, row.installation_id)
        if (
            installation is None
            or installation.suspended_at is not None
            or row.repo_full_name not in installation.repo_full_names
        ):
            continue
        result.append((grant, AuthorizedRepo.model_validate(row)))
    return result


async def get_agent_mode(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> Literal["legacy", "app"]:
    row = await session.get(AgentGitHubMode, (tenant_id, agent_id))
    return "app" if row is not None and row.mode == "app" else "legacy"


_ACCESS_RANK = {"none": 0, "read": 1, "write": 2}


async def stage_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    ceiling_access: Literal["read", "write"],
    granted_by_account_id: uuid.UUID | None,
    baseline_access: Literal["none", "read", "write"] = "none",
    mount_path: str | None = None,
    is_working_repo: bool = False,
) -> AgentGrant:
    """Stage or update a grant. The caller owns the transaction and authorizes the actor."""
    if _ACCESS_RANK[baseline_access] > _ACCESS_RANK[ceiling_access]:
        raise ValueError("baseline access exceeds ceiling access")
    if mount_path is not None and (
        not mount_path.startswith("/workspace/") or ".." in mount_path.split("/")
    ):
        raise ValueError("mount path must be inside /workspace")
    authorized = await session.get(TenantGitHubRepo, (tenant_id, repo_id), with_for_update=True)
    if authorized is None or authorized.status != "active":
        raise ValueError("repository is not actively authorized for this workspace")
    installation = await session.get(GitHubAppInstallation, authorized.installation_id)
    if installation is None or installation.suspended_at is not None:
        raise ValueError("GitHub App installation is not active")
    if _ACCESS_RANK[ceiling_access] > _ACCESS_RANK[authorized.max_access]:
        raise ValueError("ceiling access exceeds repository authorization")
    app_active = await get_agent_mode(session, tenant_id=tenant_id, agent_id=agent_id) == "app"
    if is_working_repo:
        working = await session.scalars(
            select(AgentGitHubGrant)
            .where(
                AgentGitHubGrant.tenant_id == tenant_id,
                AgentGitHubGrant.agent_id == agent_id,
                AgentGitHubGrant.is_working_repo.is_(True),
                AgentGitHubGrant.repo_id != repo_id,
            )
            .with_for_update()
        )
        for previous in working:
            previous.is_working_repo = False
            previous.version += 1
    row = await session.get(AgentGitHubGrant, (tenant_id, agent_id, repo_id), with_for_update=True)
    if row is None:
        row = AgentGitHubGrant(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            version=1,
            granted_at=datetime.now().astimezone(),
        )
        session.add(row)
    else:
        row.version += 1
    row.baseline_access = baseline_access
    row.ceiling_access = ceiling_access
    row.staged = not app_active
    row.mount_path = mount_path
    row.is_working_repo = is_working_repo
    row.granted_by_account_id = granted_by_account_id
    await session.flush()
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=granted_by_account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="github_grants",
        operation="github_grant",
        outcome="allowed",
        reason="grant updated" if app_active else "grant staged",
        github_repo_ids=[repo_id],
    )
    return AgentGrant.model_validate(row)


async def remove_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    changed_by_account_id: uuid.UUID | None = None,
) -> bool:
    row = await session.get(AgentGitHubGrant, (tenant_id, agent_id, repo_id), with_for_update=True)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=changed_by_account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="github_grants",
        operation="github_grant",
        outcome="allowed",
        reason="grant removed",
        github_repo_ids=[repo_id],
    )
    return True


async def activate_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    changed_by_account_id: uuid.UUID | None = None,
) -> None:
    """Make the staged set live and switch mode in the caller's transaction."""
    mode = await session.get(AgentGitHubMode, (tenant_id, agent_id), with_for_update=True)
    if mode is None:
        mode = AgentGitHubMode(tenant_id=tenant_id, agent_id=agent_id, mode="legacy")
        session.add(mode)
        await session.flush()
    grants = await session.scalars(
        select(AgentGitHubGrant)
        .where(AgentGitHubGrant.tenant_id == tenant_id, AgentGitHubGrant.agent_id == agent_id)
        .with_for_update()
    )
    for grant in grants:
        authorized = await session.get(TenantGitHubRepo, (tenant_id, grant.repo_id))
        if (
            authorized is None
            or authorized.status != "active"
            or (_ACCESS_RANK[grant.ceiling_access] > _ACCESS_RANK[authorized.max_access])
        ):
            raise ValueError("staged grant is no longer authorized")
        installation = await session.get(GitHubAppInstallation, authorized.installation_id)
        if installation is None or installation.suspended_at is not None:
            raise ValueError("staged grant installation is not active")
        if grant.staged:
            grant.staged = False
            grant.version += 1
    mode.mode = "app"
    await session.flush()
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=changed_by_account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="github_grants",
        operation="github_grant",
        outcome="allowed",
        reason="app mode activated",
    )


async def deactivate_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    changed_by_account_id: uuid.UUID | None = None,
) -> None:
    mode = await session.get(AgentGitHubMode, (tenant_id, agent_id), with_for_update=True)
    if mode is not None:
        mode.mode = "legacy"
        await session.flush()
        await append_event(
            session,
            tenant_id=tenant_id,
            account_id=changed_by_account_id,
            agent_id=agent_id,
            platform=None,
            platform_user_id=None,
            tool_name="github_grants",
            operation="github_grant",
            outcome="allowed",
            reason="app mode deactivated",
        )
