"""Tenant-scoped GitHub authorization records. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from daimon.core._models import AgentGitHubGrant, AgentGitHubMode, TenantGitHubRepo
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


async def get_agent_mode(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> Literal["legacy", "app"]:
    row = await session.get(AgentGitHubMode, (tenant_id, agent_id))
    return "app" if row is not None and row.mode == "app" else "legacy"
