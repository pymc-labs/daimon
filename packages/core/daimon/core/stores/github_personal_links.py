"""Single-use browser invitations for linking a chat account to GitHub."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import Account, GitHubPersonalLinkIntent, PlatformPrincipal, Tenant
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def mint_link(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    root_url: str,
) -> str:
    account = await session.get(Account, account_id)
    tenant = await session.get(Tenant, tenant_id)
    principal = await session.scalar(
        select(PlatformPrincipal).where(
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.account_id == account_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id == platform_user_id,
        )
    )
    if (
        account is None
        or account.tenant_id != tenant_id
        or account.is_external
        or tenant is None
        or tenant.platform != platform
        or principal is None
        or platform not in ("discord", "slack")
    ):
        raise ValueError("GitHub linking is unavailable here.")
    await session.execute(
        delete(GitHubPersonalLinkIntent).where(
            GitHubPersonalLinkIntent.expires_at <= datetime.now(UTC)
        )
    )
    token = secrets.token_urlsafe(32)
    session.add(
        GitHubPersonalLinkIntent(
            token_hash=digest(token),
            account_id=account_id,
            tenant_id=tenant_id,
            platform=platform,
            platform_user_id=platform_user_id,
            platform_workspace_id=tenant.external_id,
            phase="new",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await session.flush()
    return f"{root_url.rstrip('/')}/oauth/github/link/{token}"


async def get_intent(
    session: AsyncSession,
    *,
    token_hash: str,
    lock: bool = False,
    include_expired: bool = False,
) -> GitHubPersonalLinkIntent | None:
    row = await session.get(GitHubPersonalLinkIntent, token_hash, with_for_update=lock)
    if row is None or (not include_expired and row.expires_at <= datetime.now(UTC)):
        return None
    return row


async def find_by_state(
    session: AsyncSession, *, state: str, phase: str, lock: bool = False
) -> GitHubPersonalLinkIntent | None:
    column = (
        GitHubPersonalLinkIntent.platform_state
        if phase == "platform"
        else GitHubPersonalLinkIntent.github_state
    )
    query = select(GitHubPersonalLinkIntent).where(
        column == state,
        GitHubPersonalLinkIntent.phase == phase,
        GitHubPersonalLinkIntent.expires_at > datetime.now(UTC),
    )
    if lock:
        query = query.with_for_update()
    return await session.scalar(query)
