"""Factories for GitHub-OAuth-related fixtures."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import GitHubCredential, GitHubOauthState
from daimon.core.stores.domain import GitHubCredentialRow, GitHubOauthStateRow
from sqlalchemy.ext.asyncio import AsyncSession


async def make_oauth_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str = "discord",
    platform_user_id: str = "u-1",
    scopes: tuple[str, ...] = ("repo", "read:user"),
    age_minutes: int = 0,  # set >0 to synthesize an old/expired-looking row
    consumed: bool = False,  # OAuth flow removed; seed pre-consumed rows directly
) -> GitHubOauthStateRow:
    """Insert a `github_oauth_states` row.

    `age_minutes` lets tests back-date `created_at` without sleeping. The
    OAuth-flow write path (`create`/`consume`) was removed;
    this factory inserts the ORM row directly for the GDPR-purge tests that
    still need legacy-shaped rows. `consumed` sets `consumed_at` at insert
    time — there is no longer a store-level `consume()` to call.
    """
    created_at = datetime.now(tz=UTC) - timedelta(minutes=age_minutes)
    orm = GitHubOauthState(
        platform=platform,
        platform_user_id=platform_user_id,
        scopes=list(scopes),
        created_at=created_at,
        tenant_id=tenant_id,
        consumed_at=datetime.now(tz=UTC) if consumed else None,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return GitHubOauthStateRow.model_validate(orm)


async def make_github_credential(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID | None = None,
    github_login: str = "octocat",
    encrypted_token: bytes = b"fake-encrypted-bytes",
    scopes: tuple[str, ...] = ("repo", "read:user"),
) -> GitHubCredentialRow:
    pid = principal_id or uuid.uuid4()
    orm = GitHubCredential(
        principal_id=pid,
        github_login=github_login,
        encrypted_token=encrypted_token,
        scopes=list(scopes),
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return GitHubCredentialRow.model_validate(orm)
