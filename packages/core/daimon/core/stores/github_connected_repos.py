"""Server-wide connected-repo settings for chat admins."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from daimon.core._models import (
    Account,
    AgentGitHubGrant,
    AgentGitHubGrantDraft,
    GitHubAccessRequest,
    TenantGitHubRepo,
)
from daimon.core.stores.security_audit import append_event
from sqlalchemy import Exists, delete, distinct, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ConnectedRepoSummary:
    count: int
    owners: tuple[str, ...]
    agent_count: int


async def _require_admin(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> None:
    account = await session.get(Account, account_id)
    if (
        account is None
        or account.tenant_id != tenant_id
        or account.role != "admin"
        or account.is_external
    ):
        raise ValueError("Only a server or workspace admin can manage connected repos.")


def _has_own_row(model: type[AgentGitHubGrant] | type[AgentGitHubGrantDraft]) -> Exists:
    """The grant's agent has this repo connected for itself."""
    return (
        select(TenantGitHubRepo.id)
        .where(
            TenantGitHubRepo.tenant_id == model.tenant_id,
            TenantGitHubRepo.repo_id == model.repo_id,
            TenantGitHubRepo.scope_agent_id == model.agent_id,
            TenantGitHubRepo.status == "active",
        )
        .exists()
    )


async def _server_wide_row(
    session: AsyncSession, *, tenant_id: uuid.UUID, repo_id: int
) -> TenantGitHubRepo | None:
    return await session.scalar(
        select(TenantGitHubRepo)
        .where(
            TenantGitHubRepo.tenant_id == tenant_id,
            TenantGitHubRepo.repo_id == repo_id,
            TenantGitHubRepo.scope_agent_id.is_(None),
        )
        .with_for_update()
    )


@dataclass(frozen=True)
class AgentRepoSummary:
    pairs: int
    """Active repo connections that belong to one agent, counted per repo and agent."""
    agents: int
    by_agent: dict[uuid.UUID, tuple[str, ...]]
    """Each agent's own repos, by full name."""


async def agent_repo_summary(session: AsyncSession, *, tenant_id: uuid.UUID) -> AgentRepoSummary:
    """The repos connected for one agent each, for the server admins' overview."""
    rows = await session.execute(
        select(TenantGitHubRepo.scope_agent_id, TenantGitHubRepo.repo_full_name)
        .where(
            TenantGitHubRepo.tenant_id == tenant_id,
            TenantGitHubRepo.scope_agent_id.is_not(None),
            TenantGitHubRepo.status == "active",
        )
        .order_by(TenantGitHubRepo.repo_full_name)
    )
    by_agent: dict[uuid.UUID, list[str]] = {}
    pairs = 0
    for agent_id, full_name in rows:
        assert agent_id is not None
        by_agent.setdefault(agent_id, []).append(full_name)
        pairs += 1
    return AgentRepoSummary(
        pairs=pairs,
        agents=len(by_agent),
        by_agent={agent_id: tuple(names) for agent_id, names in by_agent.items()},
    )


async def summary(session: AsyncSession, *, tenant_id: uuid.UUID) -> ConnectedRepoSummary:
    repos = list(
        await session.scalars(
            select(TenantGitHubRepo).where(
                TenantGitHubRepo.tenant_id == tenant_id,
                TenantGitHubRepo.scope_agent_id.is_(None),
                TenantGitHubRepo.status == "active",
            )
        )
    )
    agent_count = await session.scalar(
        select(func.count(distinct(AgentGitHubGrant.agent_id))).where(
            AgentGitHubGrant.tenant_id == tenant_id,
            AgentGitHubGrant.staged.is_(False),
            AgentGitHubGrant.repo_id.in_([r.repo_id for r in repos]),
        )
    )
    return ConnectedRepoSummary(
        count=len(repos),
        owners=tuple(sorted({r.repo_full_name.split("/", 1)[0] for r in repos})),
        agent_count=agent_count or 0,
    )


async def set_repo_ability(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    repo_id: int,
    ability: Literal["read", "write"],
    account_id: uuid.UUID,
) -> None:
    await _require_admin(session, tenant_id=tenant_id, account_id=account_id)
    repo = await _server_wide_row(session, tenant_id=tenant_id, repo_id=repo_id)
    if repo is None or repo.status != "active":
        raise ValueError("This repo is no longer connected.")
    if ability == "write" and repo.max_access != "write":
        raise ValueError("Confirm read and write access on GitHub first.")
    repo.max_access = ability
    repo.version += 1
    if ability == "read":
        grants = await session.scalars(
            select(AgentGitHubGrant)
            .where(
                AgentGitHubGrant.tenant_id == tenant_id,
                AgentGitHubGrant.repo_id == repo_id,
                ~_has_own_row(AgentGitHubGrant),
            )
            .with_for_update()
        )
        for grant in grants:
            grant.ceiling_access = "read"
            if grant.baseline_access == "write":
                grant.baseline_access = "read"
            grant.version += 1
        drafts = await session.scalars(
            select(AgentGitHubGrantDraft)
            .where(
                AgentGitHubGrantDraft.tenant_id == tenant_id,
                AgentGitHubGrantDraft.repo_id == repo_id,
                ~_has_own_row(AgentGitHubGrantDraft),
            )
            .with_for_update()
        )
        for draft in drafts:
            if draft.operation == "upsert":
                draft.ceiling_access = "read"
                if draft.baseline_access == "write":
                    draft.baseline_access = "read"
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=None,
        platform=None,
        platform_user_id=None,
        tool_name="github_connect",
        operation="github_connect",
        outcome="allowed",
        reason="repo ability changed",
        github_repo_ids=[repo_id],
    )


async def disconnect_repo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    repo_id: int,
    account_id: uuid.UUID,
) -> None:
    await _require_admin(session, tenant_id=tenant_id, account_id=account_id)
    repo = await _server_wide_row(session, tenant_id=tenant_id, repo_id=repo_id)
    if repo is None or repo.status != "active":
        raise ValueError("This repo is no longer connected.")
    repo.status = "revoked"
    repo.status_reason = "disconnected in Daimon"
    repo.version += 1
    # An agent that has the repo connected for itself keeps it.
    await session.execute(
        delete(AgentGitHubGrant).where(
            AgentGitHubGrant.tenant_id == tenant_id,
            AgentGitHubGrant.repo_id == repo_id,
            ~_has_own_row(AgentGitHubGrant),
        )
    )
    await session.execute(
        delete(AgentGitHubGrantDraft).where(
            AgentGitHubGrantDraft.tenant_id == tenant_id,
            AgentGitHubGrantDraft.repo_id == repo_id,
            ~_has_own_row(AgentGitHubGrantDraft),
        )
    )
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=None,
        platform=None,
        platform_user_id=None,
        tool_name="github_connect",
        operation="github_connect",
        outcome="allowed",
        reason="repo disconnected",
        github_repo_ids=[repo_id],
    )


async def disconnect_github(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> int:
    """Disconnect this server's repos and cancel unfinished access requests."""
    await _require_admin(session, tenant_id=tenant_id, account_id=account_id)
    repos = list(
        await session.scalars(
            select(TenantGitHubRepo)
            .where(TenantGitHubRepo.tenant_id == tenant_id, TenantGitHubRepo.status == "active")
            .with_for_update()
        )
    )
    for repo in repos:
        repo.status = "revoked"
        repo.status_reason = "disconnected in Daimon"
        repo.version += 1
    await session.execute(delete(AgentGitHubGrant).where(AgentGitHubGrant.tenant_id == tenant_id))
    await session.execute(
        delete(AgentGitHubGrantDraft).where(AgentGitHubGrantDraft.tenant_id == tenant_id)
    )
    await session.execute(
        update(GitHubAccessRequest)
        .where(
            GitHubAccessRequest.tenant_id == tenant_id,
            GitHubAccessRequest.status.in_(("open", "waiting_github")),
        )
        .values(status="cancelled")
    )
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=None,
        platform=None,
        platform_user_id=None,
        tool_name="github_connect",
        operation="github_connect",
        outcome="allowed",
        reason="github disconnected",
        github_repo_ids=[repo.repo_id for repo in repos],
    )
    return len(repos)
