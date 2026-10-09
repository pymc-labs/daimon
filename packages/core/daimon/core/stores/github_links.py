"""GitHub identity links for later authorization flows. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from cryptography.fernet import MultiFernet
from daimon.core._models import Account, AccountGitHubLink, GitHubUserLink, PlatformPrincipal
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


async def account_link_status(session: AsyncSession, *, account_id: uuid.UUID) -> str | None:
    """Return the linked GitHub login, or None when no working link exists."""
    row = await session.scalar(
        select(GitHubUserLink.login)
        .join(AccountGitHubLink, AccountGitHubLink.github_user_id == GitHubUserLink.github_user_id)
        .where(
            AccountGitHubLink.account_id == account_id,
            GitHubUserLink.status == "active",
        )
    )
    return row


async def account_link_is_broken(session: AsyncSession, *, account_id: uuid.UUID) -> bool:
    row = await session.scalar(
        select(GitHubUserLink.github_user_id)
        .join(AccountGitHubLink, AccountGitHubLink.github_user_id == GitHubUserLink.github_user_id)
        .where(
            AccountGitHubLink.account_id == account_id,
            GitHubUserLink.status == "broken",
        )
    )
    return row is not None


async def verified_platform_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
) -> bool:
    """Recheck the web flow's account and platform identity before linking."""
    row = await session.scalar(
        select(Account.id)
        .join(PlatformPrincipal, PlatformPrincipal.account_id == Account.id)
        .where(
            Account.id == account_id,
            Account.tenant_id == tenant_id,
            Account.is_external.is_(False),
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id == platform_user_id,
        )
    )
    return row is not None


async def save_verified_link(
    session: AsyncSession,
    *,
    intent_account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    github_user_id: int,
    login: str,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    refresh_expires_in: int,
    fernet: MultiFernet,
) -> None:
    """Store the verified GitHub identity for this person on eligible accounts."""
    # Import here: github_credentials is loaded by the stores package at boot.
    from daimon.core.github_credentials import encrypt_token

    account_ids = [intent_account_id]
    if platform == "discord":
        principals = await session.scalars(
            select(PlatformPrincipal).where(
                PlatformPrincipal.platform == "discord",
                PlatformPrincipal.external_id == platform_user_id,
            )
        )
        account_ids = list({p.account_id for p in principals})
    now = datetime.now(UTC)
    user = await session.get(GitHubUserLink, github_user_id, with_for_update=True)
    if user is None:
        user = GitHubUserLink(github_user_id=github_user_id, login=login)
        session.add(user)
    else:
        user.token_generation += 1
        user.link_generation += 1
    user.login = login
    user.encrypted_access_token = encrypt_token(fernet, access_token)
    user.encrypted_refresh_token = encrypt_token(fernet, refresh_token)
    user.access_expires_at = now + timedelta(seconds=expires_in)
    user.refresh_expires_at = now + timedelta(seconds=refresh_expires_in)
    user.status = "active"
    user.linked_at = now
    await session.flush()
    for account_id in account_ids:
        account = await session.get(Account, account_id)
        if account is None or account.is_external:
            continue
        link = await session.get(AccountGitHubLink, account_id, with_for_update=True)
        if link is not None and link.github_user_id != github_user_id:
            await unlink_account(session, account_id=account_id)
            link = None
        if link is None:
            session.add(
                AccountGitHubLink(
                    account_id=account_id,
                    github_user_id=github_user_id,
                    platform=platform,
                    platform_user_id=platform_user_id,
                    verified_via=(
                        "discord_oauth"
                        if account_id == intent_account_id and platform == "discord"
                        else "auto_attach_discord"
                        if platform == "discord"
                        else "slack_oidc"
                    ),
                    linked_at=now,
                )
            )
    await session.flush()


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


async def unlink_verified_identity(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: Literal["discord", "slack"],
    platform_user_id: str,
    account_id: uuid.UUID,
) -> int:
    """Unlink this proven person everywhere their platform identity is valid."""
    caller = await session.scalar(
        select(PlatformPrincipal)
        .join(Account, Account.id == PlatformPrincipal.account_id)
        .where(
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.account_id == account_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id == platform_user_id,
            Account.is_external.is_(False),
        )
    )
    if caller is None:
        raise ValueError("GitHub linking is unavailable here.")
    query = (
        select(PlatformPrincipal.account_id)
        .join(Account, Account.id == PlatformPrincipal.account_id)
        .where(
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id == platform_user_id,
            Account.is_external.is_(False),
        )
    )
    if platform == "slack":
        query = query.where(PlatformPrincipal.tenant_id == tenant_id)
    ids = set(await session.scalars(query))
    unlinked = 0
    for linked_id in ids:
        if await unlink_account(session, account_id=linked_id) is not None:
            unlinked += 1
    return unlinked


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
