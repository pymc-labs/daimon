"""GitHub identity links for later authorization flows. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, cast

from daimon.core._models import AccountGitHubLink, GitHubUserLink
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


class GitHubUser(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    github_user_id: int
    login: str
    encrypted_access_token: bytes
    encrypted_refresh_token: bytes | None
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    token_generation: int
    link_generation: int
    status: Literal["active", "broken"]
    linked_at: datetime


class AccountLink(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    account_id: uuid.UUID
    github_user_id: int
    platform: str
    platform_user_id: str
    verified_via: Literal["discord_oauth", "slack_oidc", "auto_attach_discord"]
    linked_at: datetime


async def get_user(session: AsyncSession, *, github_user_id: int) -> GitHubUser | None:
    row = await session.get(GitHubUserLink, github_user_id)
    return GitHubUser.model_validate(row) if row is not None else None


async def get_user_for_update(session: AsyncSession, *, github_user_id: int) -> GitHubUser | None:
    row = await session.get(GitHubUserLink, github_user_id, with_for_update=True)
    return GitHubUser.model_validate(row) if row is not None else None


async def rotate_user_tokens(
    session: AsyncSession,
    *,
    github_user_id: int,
    expected_generation: int,
    encrypted_access_token: bytes,
    encrypted_refresh_token: bytes,
    access_expires_at: datetime,
    refresh_expires_at: datetime,
) -> bool:
    result = await session.execute(
        update(GitHubUserLink)
        .where(
            GitHubUserLink.github_user_id == github_user_id,
            GitHubUserLink.token_generation == expected_generation,
            GitHubUserLink.status == "active",
        )
        .values(
            encrypted_access_token=encrypted_access_token,
            encrypted_refresh_token=encrypted_refresh_token,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            token_generation=GitHubUserLink.token_generation + 1,
        )
    )
    return cast(CursorResult[Any], result).rowcount == 1


async def bump_link_generation(
    session: AsyncSession,
    *,
    github_user_id: int,
    broken: bool = False,
    expected_token_generation: int | None = None,
) -> GitHubUser | None:
    """Invalidate derived tokens; the inventory stale-token query sees the new generation."""
    conditions = [GitHubUserLink.github_user_id == github_user_id]
    if expected_token_generation is not None:
        conditions.extend(
            (
                GitHubUserLink.token_generation == expected_token_generation,
                GitHubUserLink.status == "active",
            )
        )
    row = await session.scalar(
        update(GitHubUserLink)
        .where(*conditions)
        .values(
            link_generation=GitHubUserLink.link_generation + 1,
            **({"status": "broken"} if broken else {}),
        )
        .returning(GitHubUserLink)
    )
    return GitHubUser.model_validate(row) if row is not None else None


async def get_account_link(session: AsyncSession, *, account_id: uuid.UUID) -> AccountLink | None:
    row = await session.get(AccountGitHubLink, account_id)
    return AccountLink.model_validate(row) if row is not None else None


async def unlink_account(session: AsyncSession, *, account_id: uuid.UUID) -> int | None:
    """Remove an account link and invalidate tokens minted from the user's old generation."""
    row = await session.get(AccountGitHubLink, account_id, with_for_update=True)
    if row is None:
        return None
    user_id = row.github_user_id
    await bump_link_generation(session, github_user_id=user_id)
    await session.delete(row)
    await session.flush()
    await delete_unlinked_user(session, github_user_id=user_id)
    return user_id


async def list_linked_accounts(session: AsyncSession, *, github_user_id: int) -> list[AccountLink]:
    rows = await session.scalars(
        select(AccountGitHubLink).where(AccountGitHubLink.github_user_id == github_user_id)
    )
    return [AccountLink.model_validate(row) for row in rows]


async def count_users_orphaned_by_account_delete(
    session: AsyncSession, *, account_id: uuid.UUID
) -> int:
    """Count user tokens that will lose their last account link on erasure."""
    result = await session.scalar(
        text(
            """
            SELECT count(*) FROM account_github_links AS target
            WHERE target.account_id = :account_id
              AND NOT EXISTS (
                SELECT 1 FROM account_github_links AS other
                WHERE other.github_user_id = target.github_user_id
                  AND other.account_id <> :account_id
              )
            """
        ),
        {"account_id": account_id},
    )
    return int(result or 0)


async def delete_unlinked_user(session: AsyncSession, *, github_user_id: int) -> int:
    """Delete a user's encrypted tokens only after their last account link is gone."""
    linked = (
        select(AccountGitHubLink.github_user_id)
        .where(AccountGitHubLink.github_user_id == github_user_id)
        .exists()
    )
    result = await session.execute(
        delete(GitHubUserLink).where(
            GitHubUserLink.github_user_id == github_user_id,
            ~linked,
        )
    )
    return cast(CursorResult[Any], result).rowcount
