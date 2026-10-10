"""Tenant-scoped GitHub authorization records. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, cast

from daimon.core._models import (
    Account,
    AgentGitHubGrant,
    AgentGitHubGrantDraft,
    AgentGitHubMode,
    GitHubAppInstallation,
    TenantGitHubRepo,
)
from daimon.core.stores import agent_repo_binding
from daimon.core.stores.security_audit import append_event
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession


class AuthorizedRepo(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    repo_id: int
    scope_agent_id: uuid.UUID | None = None
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


def _visible_to(agent_id: uuid.UUID | None) -> ColumnElement[bool]:
    """Server-wide rows, plus the agent's own rows when an agent is named."""
    if agent_id is None:
        return TenantGitHubRepo.scope_agent_id.is_(None)
    return or_(
        TenantGitHubRepo.scope_agent_id.is_(None), TenantGitHubRepo.scope_agent_id == agent_id
    )


async def repo_for_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    repo_id: int,
    agent_id: uuid.UUID | None,
    for_update: bool = False,
) -> TenantGitHubRepo | None:
    """The confirmation `agent_id` may use for `repo_id`: its own row, else the server-wide one.

    A row owned by another agent is never returned. `agent_id=None` reads only
    the server-wide row.
    """
    query = (
        select(TenantGitHubRepo)
        .where(
            TenantGitHubRepo.tenant_id == tenant_id,
            TenantGitHubRepo.repo_id == repo_id,
            _visible_to(agent_id),
        )
        # False sorts first: the agent's own row wins over the server-wide one.
        .order_by(TenantGitHubRepo.scope_agent_id.is_(None))
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def list_authorized_repos(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID | None = None
) -> list[AuthorizedRepo]:
    """Server-wide repos, or with `agent_id` the ones that agent may use, one row per repo."""
    rows = await session.scalars(
        select(TenantGitHubRepo)
        .where(TenantGitHubRepo.tenant_id == tenant_id, _visible_to(agent_id))
        .order_by(TenantGitHubRepo.repo_id, TenantGitHubRepo.scope_agent_id.is_(None))
    )
    picked: dict[int, AuthorizedRepo] = {}
    for row in rows:
        picked.setdefault(row.repo_id, AuthorizedRepo.model_validate(row))
    return list(picked.values())


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
        row = await repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
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


class AgentRepo(BaseModel):
    """One repo an agent has, with the confirmation it rests on."""

    model_config = ConfigDict(frozen=True)
    repo_id: int
    full_name: str
    # "agent": connected for this agent only. "shared": a server-wide repo.
    scope: Literal["agent", "shared"]
    max_access: Literal["read", "write"]
    baseline_access: Literal["none", "read", "write"]
    ceiling_access: Literal["read", "write"]
    staged: bool
    status: Literal["active", "suspended", "revoked"]
    added_by_account_id: uuid.UUID | None
    added_at: datetime
    granted_by_account_id: uuid.UUID | None
    granted_at: datetime


async def list_agent_repos(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[AgentRepo]:
    """The agent's granted repos, each read from the row it may use."""
    result: list[AgentRepo] = []
    for grant in await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id):
        row = await repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
        if row is None:
            continue
        result.append(
            AgentRepo(
                repo_id=row.repo_id,
                full_name=row.repo_full_name,
                scope="shared" if row.scope_agent_id is None else "agent",
                max_access=cast(Literal["read", "write"], row.max_access),
                baseline_access=grant.baseline_access,
                ceiling_access=grant.ceiling_access,
                staged=grant.staged,
                status=cast(Literal["active", "suspended", "revoked"], row.status),
                added_by_account_id=row.authorized_by_account_id,
                added_at=row.authorized_at,
                granted_by_account_id=grant.granted_by_account_id,
                granted_at=grant.granted_at,
            )
        )
    return sorted(result, key=lambda repo: repo.full_name.casefold())


async def set_working_repo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_name: str | None,
    account_id: uuid.UUID,
) -> str | None:
    """Select one live grant as the filesystem repo, or clear the selection.

    The caller verifies that the account manages this agent. Locking the
    agent's grant rows serializes changes to an existing grant set.
    """
    rows = list(
        await session.scalars(
            select(AgentGitHubGrant)
            .where(AgentGitHubGrant.tenant_id == tenant_id, AgentGitHubGrant.agent_id == agent_id)
            .order_by(AgentGitHubGrant.repo_id)
            .with_for_update()
        )
    )
    selected: AgentGitHubGrant | None = None
    selected_name: str | None = None
    if repo_name is not None:
        for grant in rows:
            repo = await repo_for_agent(
                session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
            )
            if repo is None or repo.status != "active":
                continue
            installation = await session.get(GitHubAppInstallation, repo.installation_id)
            if installation is None or installation.suspended_at is not None:
                continue
            if repo.repo_full_name.casefold() == repo_name.casefold():
                selected, selected_name = grant, repo.repo_full_name
                break
        if selected is None:
            raise ValueError("That repo is not on this agent's list. Offer Connect GitHub.")
    changed_ids: list[int] = []
    cleared_binding = False
    if selected is None:
        binding = await agent_repo_binding.get_binding(
            session, tenant_id=tenant_id, agent_id=agent_id
        )
        if binding is not None:
            await agent_repo_binding.clear_binding(session, tenant_id=tenant_id, agent_id=agent_id)
            cleared_binding = True
    for grant in rows:
        wanted = grant is selected
        if grant.is_working_repo != wanted:
            grant.is_working_repo = wanted
            grant.version += 1
            changed_ids.append(grant.repo_id)
    await session.flush()
    if changed_ids or cleared_binding:
        await append_event(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            platform=None,
            platform_user_id=None,
            tool_name="set_working_repo",
            operation="github_grant",
            outcome="allowed",
            reason="working repo selected" if selected else "working repo cleared",
            github_repo_ids=changed_ids,
        )
    return selected_name


async def remove_agent_repo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    account_id: uuid.UUID | None,
) -> bool:
    """Take a repo away from one agent now. The caller checks they manage the agent.

    Removes the grant and any pending draft. A repo connected for this agent
    only is disconnected too, since no other agent may use it; adding it back
    needs a new Connect. A server-wide repo stays connected for other agents.
    """
    removed = await remove_grant(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=repo_id,
        changed_by_account_id=account_id,
    )
    draft = await session.get(AgentGitHubGrantDraft, (tenant_id, agent_id, repo_id))
    if draft is not None:
        await session.delete(draft)
        removed = True
    own = await session.scalar(
        select(TenantGitHubRepo)
        .where(
            TenantGitHubRepo.tenant_id == tenant_id,
            TenantGitHubRepo.repo_id == repo_id,
            TenantGitHubRepo.scope_agent_id == agent_id,
            TenantGitHubRepo.status == "active",
        )
        .with_for_update()
    )
    if own is not None:
        own.status = "revoked"
        own.status_reason = "removed from the agent in Daimon"
        own.version += 1
        removed = True
        await append_event(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            platform=None,
            platform_user_id=None,
            tool_name="github_grants",
            operation="github_connect",
            outcome="allowed",
            reason="agent repo disconnected",
            github_repo_ids=[repo_id],
        )
    await session.flush()
    return removed


async def get_agent_mode(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> Literal["legacy", "app"]:
    row = await session.get(AgentGitHubMode, (tenant_id, agent_id))
    return "app" if row is not None and row.mode == "app" else "legacy"


_ACCESS_RANK = {"none": 0, "read": 1, "write": 2}

SERVER_WIDE_GRANT_MESSAGE = "Only a server admin can add a repo connected for the whole server."


async def require_may_grant(
    session: AsyncSession, *, repo: TenantGitHubRepo, account_id: uuid.UUID | None
) -> None:
    """A server-wide repo is a server admin's to grant; an agent's own repo, its manager's.

    `account_id=None` is the operator CLI. Whether the account manages the
    agent at all is the caller's check.
    """
    if repo.scope_agent_id is not None or account_id is None:
        return
    account = await session.get(Account, account_id)
    if account is None or account.role != "admin" or account.is_external:
        raise ValueError(SERVER_WIDE_GRANT_MESSAGE)


async def stage_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    baseline_access: Literal["none", "read", "write"],
    ceiling_access: Literal["read", "write"],
    granted_by_account_id: uuid.UUID | None,
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
    authorized = await repo_for_agent(
        session, tenant_id=tenant_id, repo_id=repo_id, agent_id=agent_id, for_update=True
    )
    if authorized is None or authorized.status != "active":
        raise ValueError("repository is not actively authorized for this workspace")
    await require_may_grant(session, repo=authorized, account_id=granted_by_account_id)
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
        authorized = await repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
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
