"""Single-use GitHub connection invitations and browser flow records."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from daimon.core._models import (
    Account,
    CliPrincipal,
    GitHubConnectFlow,
    GitHubConnectInvitation,
    Tenant,
    TenantGitHubOrgScope,
    TenantGitHubRepo,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def cli_account_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, os_user: str
) -> uuid.UUID | None:
    return await session.scalar(
        select(CliPrincipal.account_id).where(
            CliPrincipal.tenant_id == tenant_id, CliPrincipal.os_user == os_user
        )
    )


class Invitation(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    token_hash: str
    tenant_id: uuid.UUID
    requester_account_id: uuid.UUID
    workspace_label: str
    requester_label: str
    expires_at: datetime
    used_at: datetime | None


class Flow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    state_hash: str
    invitation_hash: str
    cookie_hash: str
    encrypted_verifier: bytes
    encrypted_user_token: bytes | None
    github_user_id: int | None
    expires_at: datetime


async def mint_invitation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    workspace_label: str | None = None,
    requester_label: str | None = None,
) -> str:
    account = await session.get(Account, requester_account_id)
    if account is None or account.tenant_id != tenant_id or account.role != "admin":
        raise ValueError("requester must be a tenant admin")
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError("workspace not found")
    token = secrets.token_urlsafe(32)
    session.add(
        GitHubConnectInvitation(
            token_hash=digest(token),
            tenant_id=tenant_id,
            requester_account_id=requester_account_id,
            workspace_label=workspace_label or f"{tenant.external_id} ({tenant.platform})",
            requester_label=requester_label or str(requester_account_id),
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await session.flush()
    return token


async def get_invitation(session: AsyncSession, token_hash: str) -> Invitation | None:
    row = await session.get(GitHubConnectInvitation, token_hash)
    if row is None or row.used_at is not None or row.expires_at <= datetime.now(UTC):
        return None
    return Invitation.model_validate(row)


async def create_flow(
    session: AsyncSession,
    *,
    invitation_hash: str,
    state: str,
    cookie: str,
    encrypted_verifier: bytes,
) -> None:
    await session.execute(
        delete(GitHubConnectFlow).where(GitHubConnectFlow.expires_at <= datetime.now(UTC))
    )
    session.add(
        GitHubConnectFlow(
            state_hash=digest(state),
            invitation_hash=invitation_hash,
            cookie_hash=digest(cookie),
            encrypted_verifier=encrypted_verifier,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    await session.flush()


async def delete_expired_flows(session: AsyncSession, *, now: datetime, limit: int = 500) -> int:
    """Remove expired encrypted browser tokens in bounded scheduler batches."""
    expired = (
        select(GitHubConnectFlow.state_hash).where(GitHubConnectFlow.expires_at <= now).limit(limit)
    )
    result = await session.execute(
        delete(GitHubConnectFlow).where(GitHubConnectFlow.state_hash.in_(expired))
    )
    return cast(CursorResult[Any], result).rowcount


async def get_flow(session: AsyncSession, *, state: str, cookie: str) -> Flow | None:
    if not state or not cookie:
        return None
    row = await session.get(GitHubConnectFlow, digest(state))
    if row is None or row.cookie_hash != digest(cookie) or row.expires_at <= datetime.now(UTC):
        return None
    if await get_invitation(session, row.invitation_hash) is None:
        return None
    return Flow.model_validate(row)


async def set_user_token(
    session: AsyncSession, *, state: str, encrypted_token: bytes, github_user_id: int
) -> bool:
    row = await session.get(GitHubConnectFlow, digest(state), with_for_update=True)
    if row is None or row.expires_at <= datetime.now(UTC) or row.encrypted_user_token is not None:
        return False
    row.encrypted_user_token = encrypted_token
    row.github_user_id = github_user_id
    await session.flush()
    return True


class RepoConfirmation(BaseModel):
    repo_id: int
    owner_id: int
    installation_id: int
    full_name: str
    max_access: Literal["read", "write"]


class OrgConfirmation(BaseModel):
    owner_id: int
    installation_id: int
    owner_login: str
    max_access: Literal["read", "write"]


class OrgScope(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    owner_id: int
    installation_id: int
    owner_login: str
    scope: Literal["org_all"]
    max_access: Literal["read", "write"]


async def get_org_scope(
    session: AsyncSession, *, tenant_id: uuid.UUID, owner_id: int
) -> OrgScope | None:
    row = await session.get(TenantGitHubOrgScope, (tenant_id, owner_id))
    return OrgScope.model_validate(row) if row is not None else None


async def confirm(
    session: AsyncSession,
    *,
    state: str,
    cookie: str,
    github_user_id: int,
    repos: list[RepoConfirmation],
    orgs: list[OrgConfirmation],
) -> bool:
    """Consume the invitation and write confirmed rows in the caller's transaction."""
    flow = await session.get(GitHubConnectFlow, digest(state), with_for_update=True)
    if flow is None or flow.cookie_hash != digest(cookie) or flow.expires_at <= datetime.now(UTC):
        return False
    invitation = await session.get(
        GitHubConnectInvitation, flow.invitation_hash, with_for_update=True
    )
    if (
        invitation is None
        or invitation.used_at is not None
        or invitation.expires_at <= datetime.now(UTC)
    ):
        return False
    account = await session.get(Account, invitation.requester_account_id, with_for_update=True)
    if account is None or account.tenant_id != invitation.tenant_id or account.role != "admin":
        return False
    if len({repo.repo_id for repo in repos}) != len(repos) or len(
        {org.owner_id for org in orgs}
    ) != len(orgs):
        return False
    now = datetime.now(UTC)
    for repo in repos:
        existing = await session.get(TenantGitHubRepo, (invitation.tenant_id, repo.repo_id))
        if existing is None:
            existing = TenantGitHubRepo(tenant_id=invitation.tenant_id, repo_id=repo.repo_id)
            session.add(existing)
        else:
            existing.version += 1
        existing.owner_id = repo.owner_id
        existing.installation_id = repo.installation_id
        existing.repo_full_name = repo.full_name
        existing.max_access = repo.max_access
        existing.authorized_by_github_user_id = github_user_id
        existing.authorized_by_account_id = invitation.requester_account_id
        existing.authorized_at = now
        existing.status = "active"
        existing.status_reason = None
    for org in orgs:
        existing_org = await session.get(TenantGitHubOrgScope, (invitation.tenant_id, org.owner_id))
        if existing_org is None:
            existing_org = TenantGitHubOrgScope(
                tenant_id=invitation.tenant_id, owner_id=org.owner_id
            )
            session.add(existing_org)
        existing_org.installation_id = org.installation_id
        existing_org.owner_login = org.owner_login
        existing_org.scope = "org_all"
        existing_org.max_access = org.max_access
        existing_org.authorized_by_github_user_id = github_user_id
        existing_org.authorized_by_account_id = invitation.requester_account_id
        existing_org.authorized_at = now
    invitation.used_at = now
    await session.delete(flow)
    await session.flush()
    return True
