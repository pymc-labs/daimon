"""Server-wide connected-repo settings for chat admins."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core._models import (
    AgentGitHubGrant,
    AgentGitHubGrantDraft,
    TenantGitHubRepo,
)
from sqlalchemy import Exists, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased


@dataclass(frozen=True)
class ConnectedRepoSummary:
    count: int
    owners: tuple[str, ...]
    agent_count: int


def _has_own_row(model: type[AgentGitHubGrant] | type[AgentGitHubGrantDraft]) -> Exists:
    """The grant's agent has this repo connected for itself."""
    own = aliased(TenantGitHubRepo)
    return (
        select(own.id)
        .where(
            own.tenant_id == model.tenant_id,
            own.repo_id == model.repo_id,
            own.scope_agent_id == model.agent_id,
            own.status == "active",
        )
        .exists()
    )


@dataclass(frozen=True)
class AgentRepoSummary:
    pairs: int
    """Repo and agent pairs: each repo an agent can use, counted once per agent."""
    agents: int
    by_agent: dict[uuid.UUID, tuple[str, ...]]
    """Every repo each agent can use by full name: its own and its server-wide grants."""


async def agent_repo_summary(session: AsyncSession, *, tenant_id: uuid.UUID) -> AgentRepoSummary:
    """The repos each agent can use, for the server admins' overview."""
    by_agent: dict[uuid.UUID, set[str]] = {}
    own = await session.execute(
        select(TenantGitHubRepo.scope_agent_id, TenantGitHubRepo.repo_full_name).where(
            TenantGitHubRepo.tenant_id == tenant_id,
            TenantGitHubRepo.scope_agent_id.is_not(None),
            TenantGitHubRepo.status == "active",
        )
    )
    for agent_id, full_name in own:
        assert agent_id is not None
        by_agent.setdefault(agent_id, set()).add(full_name)
    shared = await session.execute(
        select(AgentGitHubGrant.agent_id, TenantGitHubRepo.repo_full_name)
        .join(
            TenantGitHubRepo,
            (TenantGitHubRepo.tenant_id == AgentGitHubGrant.tenant_id)
            & (TenantGitHubRepo.repo_id == AgentGitHubGrant.repo_id)
            & TenantGitHubRepo.scope_agent_id.is_(None),
        )
        .where(
            AgentGitHubGrant.tenant_id == tenant_id,
            AgentGitHubGrant.app_visible.is_(True),
            TenantGitHubRepo.status == "active",
            ~_has_own_row(AgentGitHubGrant),
        )
    )
    for agent_id, full_name in shared:
        by_agent.setdefault(agent_id, set()).add(full_name)
    return AgentRepoSummary(
        pairs=sum(len(names) for names in by_agent.values()),
        agents=len(by_agent),
        by_agent={
            agent_id: tuple(sorted(names, key=str.casefold)) for agent_id, names in by_agent.items()
        },
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
            AgentGitHubGrant.app_visible.is_(True),
            AgentGitHubGrant.repo_id.in_([r.repo_id for r in repos]),
        )
    )
    return ConnectedRepoSummary(
        count=len(repos),
        owners=tuple(sorted({r.repo_full_name.split("/", 1)[0] for r in repos})),
        agent_count=agent_count or 0,
    )
