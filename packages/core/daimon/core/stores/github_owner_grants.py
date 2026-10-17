"""Agent owner grants and durable per-repository exclusions."""

from __future__ import annotations

import uuid
from typing import Literal

from daimon.core._models import (
    AgentGitHubOwnerExclusion,
    AgentGitHubOwnerGrant,
    GitHubAppInstallation,
    TenantGitHubRepo,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


class OwnerGrant(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    installation_id: int
    owner_login: str
    access: Literal["read", "write"]


class OwnerRepository(BaseModel):
    model_config = ConfigDict(frozen=True)
    installation_id: int
    full_name: str
    access: Literal["read", "write"]


async def installation_associated(
    session: AsyncSession, *, tenant_id: uuid.UUID, installation_id: int
) -> bool:
    return (
        await session.scalar(
            select(TenantGitHubRepo.repo_id)
            .where(
                TenantGitHubRepo.tenant_id == tenant_id,
                TenantGitHubRepo.installation_id == installation_id,
            )
            .limit(1)
        )
        is not None
    )


async def upsert_owner_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    installation_id: int,
    owner_login: str,
    access: Literal["read", "write"],
    account_id: uuid.UUID,
) -> None:
    stmt = (
        insert(AgentGitHubOwnerGrant)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            installation_id=installation_id,
            owner_login=owner_login,
            access=access,
            granted_by_account_id=account_id,
        )
        .on_conflict_do_update(
            index_elements=["tenant_id", "agent_id", "installation_id"],
            set_={
                "owner_login": owner_login,
                "access": access,
                "granted_by_account_id": account_id,
            },
        )
    )
    await session.execute(stmt)


async def list_owner_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[OwnerGrant]:
    rows = await session.scalars(
        select(AgentGitHubOwnerGrant).where(
            AgentGitHubOwnerGrant.tenant_id == tenant_id,
            AgentGitHubOwnerGrant.agent_id == agent_id,
        )
    )
    return [OwnerGrant.model_validate(row) for row in rows]


async def has_owner_grant(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, owner_login: str
) -> bool:
    return any(
        row.owner_login.casefold() == owner_login.casefold()
        for row in await list_owner_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    )


async def remove_owner_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, owner_login: str
) -> bool:
    grants = [
        row
        for row in await list_owner_grants(session, tenant_id=tenant_id, agent_id=agent_id)
        if row.owner_login.casefold() == owner_login.casefold()
    ]
    for row in grants:
        await session.execute(
            delete(AgentGitHubOwnerGrant).where(
                AgentGitHubOwnerGrant.tenant_id == tenant_id,
                AgentGitHubOwnerGrant.agent_id == agent_id,
                AgentGitHubOwnerGrant.installation_id == row.installation_id,
            )
        )
    return bool(grants)


async def exclude_repo(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, repo_full_name: str
) -> None:
    repo_full_name = repo_full_name.casefold()
    await session.execute(
        insert(AgentGitHubOwnerExclusion)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_full_name=repo_full_name,
        )
        .on_conflict_do_nothing()
    )


async def clear_exclusion(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, repo_full_name: str
) -> None:
    repo_full_name = repo_full_name.casefold()
    await session.execute(
        delete(AgentGitHubOwnerExclusion).where(
            AgentGitHubOwnerExclusion.tenant_id == tenant_id,
            AgentGitHubOwnerExclusion.agent_id == agent_id,
            func.lower(AgentGitHubOwnerExclusion.repo_full_name) == repo_full_name,
        )
    )


async def resolve_owner_repositories(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[OwnerRepository]:
    excluded = {
        name.casefold()
        for name in await session.scalars(
            select(AgentGitHubOwnerExclusion.repo_full_name).where(
                AgentGitHubOwnerExclusion.tenant_id == tenant_id,
                AgentGitHubOwnerExclusion.agent_id == agent_id,
            )
        )
    }
    result: list[OwnerRepository] = []
    for grant in await list_owner_grants(session, tenant_id=tenant_id, agent_id=agent_id):
        installation = await session.get(GitHubAppInstallation, grant.installation_id)
        if (
            installation is None
            or installation.suspended_at is not None
            or installation.github_removed_at is not None
        ):
            continue
        names = (
            installation.github_repo_full_names
            if installation.github_repo_full_names is not None
            else installation.repo_full_names
        )
        for name in names:
            if (
                name.split("/", 1)[0].casefold() == grant.owner_login.casefold()
                and name.casefold() not in excluded
            ):
                result.append(
                    OwnerRepository(
                        installation_id=grant.installation_id, full_name=name, access=grant.access
                    )
                )
    return result
