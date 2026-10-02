"""Durable, encrypted installation-token inventory. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from cryptography.fernet import MultiFernet
from daimon.core._models import (
    AccountGitHubLink,
    AgentGitHubGrant,
    GitHubAppInstallation,
    GitHubIssuedToken,
    GitHubUserLink,
    TenantGitHubRepo,
)
from daimon.core.github_credentials import decrypt_token, encrypt_token
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class IssuedToken(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    token_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: str
    installation_id: int
    repo_ids: list[int]
    permissions: dict[str, str]
    grant_versions: dict[str, int]
    requester_account_id: uuid.UUID | None
    link_generation: int | None
    encrypted_token: bytes | None
    expires_at: datetime
    status: Literal["pending", "stored", "delivered", "revoked"]
    revoked_at: datetime | None
    revoke_attempts: int


async def create_pending(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: str,
    installation_id: int,
    repo_ids: list[int],
    permissions: dict[str, str],
    grant_versions: dict[str, int],
    expires_at: datetime,
    requester_account_id: uuid.UUID | None = None,
    link_generation: int | None = None,
) -> IssuedToken:
    if not repo_ids or len(repo_ids) > 500 or len(set(repo_ids)) != len(repo_ids):
        raise ValueError("token inventory requires 1..500 repository IDs")
    if any(
        repo_id <= 0
        or grant_versions.get(f"grant:{repo_id}", 0) <= 0
        or grant_versions.get(f"authorization:{repo_id}", 0) <= 0
        for repo_id in repo_ids
    ):
        raise ValueError("token inventory requires positive IDs and both recorded versions")
    if expires_at.utcoffset() is None:
        raise ValueError("expires_at must include a timezone")
    row = GitHubIssuedToken(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        installation_id=installation_id,
        repo_ids=repo_ids,
        permissions=permissions,
        grant_versions=grant_versions,
        requester_account_id=requester_account_id,
        link_generation=link_generation,
        expires_at=expires_at,
        status="pending",
        revoke_attempts=0,
    )
    session.add(row)
    await session.flush()
    return IssuedToken.model_validate(row)


async def store_token(
    session: AsyncSession, *, token_id: uuid.UUID, token: str, fernet: MultiFernet
) -> IssuedToken:
    row = await session.get(GitHubIssuedToken, token_id, with_for_update=True)
    if row is None or row.status != "pending":
        raise ValueError("token is not pending")
    row.encrypted_token = encrypt_token(fernet, token)
    row.status = "stored"
    await session.flush()
    return IssuedToken.model_validate(row)


async def mark_delivered(session: AsyncSession, *, token_id: uuid.UUID) -> IssuedToken:
    row = await session.get(GitHubIssuedToken, token_id, with_for_update=True)
    if row is None or row.status != "stored":
        raise ValueError("token is not stored")
    row.status = "delivered"
    await session.flush()
    return IssuedToken.model_validate(row)


async def mark_revoked(session: AsyncSession, *, token_id: uuid.UUID) -> IssuedToken:
    row = await session.get(GitHubIssuedToken, token_id, with_for_update=True)
    if row is None or row.status == "pending":
        raise ValueError("token cannot be revoked")
    row.status = "revoked"
    row.revoked_at = datetime.now(UTC)
    row.revoke_attempts += 1
    await session.flush()
    return IssuedToken.model_validate(row)


async def record_revoke_attempt(session: AsyncSession, *, token_id: uuid.UUID) -> None:
    row = await session.get(GitHubIssuedToken, token_id, with_for_update=True)
    if row is None:
        raise ValueError("unknown token")
    row.revoke_attempts += 1
    await session.flush()


def decrypt_issued_token(row: IssuedToken, *, fernet: MultiFernet) -> str | None:
    return decrypt_token(fernet, row.encrypted_token) if row.encrypted_token is not None else None


async def select_stale_tokens(
    session: AsyncSession, *, now: datetime | None = None
) -> list[IssuedToken]:
    """Find live issued tokens whose grant, authorization or requester link changed."""
    current = now or datetime.now(UTC)
    rows = await session.scalars(
        select(GitHubIssuedToken).where(
            GitHubIssuedToken.status.in_(("stored", "delivered")),
            GitHubIssuedToken.expires_at > current,
        )
    )
    stale: list[IssuedToken] = []
    for row in rows:
        installation = await session.get(GitHubAppInstallation, row.installation_id)
        outdated = installation is None or installation.suspended_at is not None
        for repo_id in row.repo_ids:
            grant = await session.get(AgentGitHubGrant, (row.tenant_id, row.agent_id, repo_id))
            authorization = await session.get(TenantGitHubRepo, (row.tenant_id, repo_id))
            if (
                grant is None
                or grant.staged
                or authorization is None
                or authorization.status != "active"
                or authorization.installation_id != row.installation_id
                or row.grant_versions.get(f"grant:{repo_id}") != grant.version
                or row.grant_versions.get(f"authorization:{repo_id}") != authorization.version
            ):
                outdated = True
                break
        if row.link_generation is not None:
            link = await session.scalar(
                select(GitHubUserLink)
                .join(
                    AccountGitHubLink,
                    GitHubUserLink.github_user_id == AccountGitHubLink.github_user_id,
                )
                .where(AccountGitHubLink.account_id == row.requester_account_id)
            )
            if (
                link is None
                or link.link_generation != row.link_generation
                or link.status != "active"
            ):
                outdated = True
        if outdated:
            stale.append(IssuedToken.model_validate(row))
    return stale
