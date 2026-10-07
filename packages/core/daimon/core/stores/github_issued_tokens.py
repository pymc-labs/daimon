"""Durable, encrypted installation-token inventory. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from cryptography.fernet import MultiFernet
from daimon.core._models import GitHubAppHeadlessSession, GitHubIssuedToken, ThreadSession
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.stores.domain import ThreadSessionRow
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select, text, update
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
    github_user_id: int | None
    link_generation: int | None
    encrypted_token: bytes | None
    expires_at: datetime
    status: Literal["pending", "stored", "delivered", "revoked"]
    revoked_at: datetime | None
    revoke_attempts: int
    superseded_at: datetime | None
    revoke_after: datetime | None
    created_at: datetime


class GitHubTokenRowClosedError(ValueError):
    """The issued-token row was closed before its minted token could be stored."""


class LiveAppSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    mapping: ThreadSessionRow
    agent_id: uuid.UUID
    expires_at: datetime
    has_linked_requester: bool
    permissions_by_repo: dict[int, dict[str, str]]


class ClosedAppSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    session_id: str
    vault_id: str | None


async def erase_requester_identity(session: AsyncSession, *, account_id: uuid.UUID) -> None:
    """Remove the GitHub user ID before account deletion clears the requester FK."""
    await session.execute(
        update(GitHubIssuedToken)
        .where(GitHubIssuedToken.requester_account_id == account_id)
        .values(github_user_id=None)
    )


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
    github_user_id: int | None = None,
    link_generation: int | None = None,
) -> IssuedToken:
    if (requester_account_id is None) != (github_user_id is None) or (github_user_id is None) != (
        link_generation is None
    ):
        raise ValueError("requester link identity and generation must be recorded together")
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
        github_user_id=github_user_id,
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
    """Store a minted token; if revoked, the minter must DELETE /installation/token."""
    row = await session.get(GitHubIssuedToken, token_id, with_for_update=True)
    if row is not None and row.status == "revoked":
        raise GitHubTokenRowClosedError("token row was closed before storage")
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
    if row is None:
        raise ValueError("unknown token")
    if row.status == "revoked":
        return IssuedToken.model_validate(row)
    row.status = "revoked"
    row.revoked_at = datetime.now(UTC)
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
    """Find live tokens with changed grant, authorization or requester link state."""
    current = now or datetime.now(UTC)
    statement = (
        select(GitHubIssuedToken)
        .from_statement(
            text(
                """
            SELECT token.*
            FROM github_issued_tokens AS token
            LEFT JOIN github_app_installations AS installation
              ON installation.installation_id = token.installation_id
            LEFT JOIN account_github_links AS account_link
              ON account_link.account_id = token.requester_account_id
            LEFT JOIN github_user_links AS user_link
              ON user_link.github_user_id = account_link.github_user_id
            WHERE token.status IN ('stored', 'delivered')
              AND token.expires_at > :now
              AND (token.revoke_after IS NULL OR token.revoke_after <= :now)
              AND (
                installation.installation_id IS NULL
                OR installation.suspended_at IS NOT NULL
                OR (
                  token.link_generation IS NOT NULL
                  AND (
                    user_link.github_user_id IS NULL
                    OR user_link.github_user_id IS DISTINCT FROM token.github_user_id
                    OR user_link.status <> 'active'
                    OR user_link.link_generation IS DISTINCT FROM token.link_generation
                  )
                )
                OR EXISTS (
                  SELECT 1
                  FROM unnest(token.repo_ids) AS repo(repo_id)
                  LEFT JOIN agent_github_grants AS grant_row
                    ON grant_row.tenant_id = token.tenant_id
                   AND grant_row.agent_id = token.agent_id
                   AND grant_row.repo_id = repo.repo_id
                  LEFT JOIN tenant_github_repos AS auth_row
                    ON auth_row.tenant_id = token.tenant_id
                   AND auth_row.repo_id = repo.repo_id
                  WHERE grant_row.repo_id IS NULL
                     OR grant_row.staged
                     OR auth_row.repo_id IS NULL
                     OR auth_row.status <> 'active'
                     OR auth_row.installation_id <> token.installation_id
                     OR NOT (auth_row.repo_full_name = ANY(installation.repo_full_names))
                     OR token.grant_versions ->> ('grant:' || repo.repo_id::text)
                        IS DISTINCT FROM grant_row.version::text
                     OR token.grant_versions ->> ('authorization:' || repo.repo_id::text)
                        IS DISTINCT FROM auth_row.version::text
                )
              )
            """
            )
        )
        .execution_options(populate_existing=True)
    )
    rows = await session.scalars(statement, {"now": current})
    return [IssuedToken.model_validate(row) for row in rows]


async def set_session_id(
    session: AsyncSession, *, provisional_session_id: str, session_id: str
) -> None:
    await session.execute(
        update(GitHubIssuedToken)
        .where(GitHubIssuedToken.session_id == provisional_session_id)
        .values(session_id=session_id)
    )


async def list_session_tokens(session: AsyncSession, *, session_id: str) -> list[IssuedToken]:
    rows = await session.scalars(
        select(GitHubIssuedToken).where(
            GitHubIssuedToken.session_id == session_id,
            GitHubIssuedToken.status.in_(("stored", "delivered")),
        )
    )
    return [IssuedToken.model_validate(row) for row in rows]


async def mark_session_tokens_superseded(
    session: AsyncSession, *, session_id: str, except_ids: frozenset[uuid.UUID], now: datetime
) -> None:
    """Keep old credentials valid while a vault update propagates."""
    await session.execute(
        update(GitHubIssuedToken)
        .where(
            GitHubIssuedToken.session_id == session_id,
            GitHubIssuedToken.status == "delivered",
            GitHubIssuedToken.superseded_at.is_(None),
            GitHubIssuedToken.token_id.not_in(except_ids),
        )
        .values(superseded_at=now, revoke_after=now + timedelta(seconds=120))
    )


async def select_due_superseded_tokens(
    session: AsyncSession, *, now: datetime | None = None
) -> list[IssuedToken]:
    rows = await session.scalars(
        select(GitHubIssuedToken).where(
            GitHubIssuedToken.status == "delivered",
            GitHubIssuedToken.revoke_after <= (now or datetime.now(UTC)),
        )
    )
    return [IssuedToken.model_validate(row) for row in rows]


async def select_abandoned_pending_tokens(
    session: AsyncSession, *, now: datetime | None = None
) -> list[IssuedToken]:
    rows = await session.scalars(
        select(GitHubIssuedToken).where(
            GitHubIssuedToken.status == "stored",
            GitHubIssuedToken.session_id.startswith("pending:"),
            GitHubIssuedToken.created_at <= (now or datetime.now(UTC)) - timedelta(minutes=2),
        )
    )
    return [IssuedToken.model_validate(row) for row in rows]


async def register_headless_app_session(
    session: AsyncSession, *, session_id: str, vault_id: str
) -> None:
    session.add(GitHubAppHeadlessSession(session_id=session_id, vault_id=vault_id))
    await session.flush()


async def finish_headless_app_session(session: AsyncSession, *, session_id: str) -> str | None:
    row = await session.get(GitHubAppHeadlessSession, session_id, with_for_update=True)
    if row is None:
        return None
    if row.finished_at is None:
        row.finished_at = datetime.now(UTC)
    await session.flush()
    return row.vault_id


async def mark_headless_app_session_closed(session: AsyncSession, *, session_id: str) -> None:
    row = await session.get(GitHubAppHeadlessSession, session_id, with_for_update=True)
    if row is not None and row.closed_at is None:
        row.closed_at = datetime.now(UTC)
        await session.flush()


async def select_deactivated_tokens(session: AsyncSession) -> list[IssuedToken]:
    rows = await session.scalars(
        select(GitHubIssuedToken).from_statement(
            text(
                """SELECT token.* FROM github_issued_tokens AS token
                LEFT JOIN agent_github_mode AS mode
                  ON mode.tenant_id = token.tenant_id AND mode.agent_id = token.agent_id
                WHERE token.status IN ('stored', 'delivered')
                  AND mode.mode IS DISTINCT FROM 'app'"""
            )
        )
    )
    return [IssuedToken.model_validate(row) for row in rows]


async def list_live_app_sessions(session: AsyncSession) -> list[LiveAppSession]:
    rows = await session.execute(
        select(ThreadSession, GitHubIssuedToken)
        .join(GitHubIssuedToken, GitHubIssuedToken.session_id == ThreadSession.ma_session_id)
        .where(
            ThreadSession.status == "live",
            GitHubIssuedToken.status == "delivered",
            GitHubIssuedToken.superseded_at.is_(None),
        )
    )
    grouped: dict[str, LiveAppSession] = {}
    for mapping, token in rows:
        mapped = ThreadSessionRow.model_validate(mapping)
        if mapped.effective_config is None or mapped.effective_config.github_mode != "app":
            continue
        current = grouped.get(mapping.ma_session_id)
        if current is None:
            grouped[mapping.ma_session_id] = LiveAppSession(
                mapping=mapped,
                agent_id=token.agent_id,
                expires_at=token.expires_at,
                has_linked_requester=token.link_generation is not None,
                permissions_by_repo={repo_id: token.permissions for repo_id in token.repo_ids},
            )
        else:
            grouped[mapping.ma_session_id] = current.model_copy(
                update={
                    "expires_at": min(current.expires_at, token.expires_at),
                    "has_linked_requester": current.has_linked_requester
                    or token.link_generation is not None,
                    "permissions_by_repo": {
                        **current.permissions_by_repo,
                        **{repo_id: token.permissions for repo_id in token.repo_ids},
                    },
                }
            )
    return list(grouped.values())


async def list_closed_app_sessions(
    session: AsyncSession, *, now: datetime
) -> list[ClosedAppSession]:
    """Delivered tokens whose MA mapping ended, after a one-minute create grace."""
    ids = await session.scalars(
        select(GitHubIssuedToken.session_id)
        .where(
            GitHubIssuedToken.status == "delivered",
            GitHubIssuedToken.expires_at <= now + timedelta(minutes=54),
        )
        .distinct()
    )
    result: list[ClosedAppSession] = []
    headless_rows = await session.scalars(
        select(GitHubAppHeadlessSession).where(
            or_(
                GitHubAppHeadlessSession.finished_at.is_not(None),
                GitHubAppHeadlessSession.created_at <= now - timedelta(minutes=46),
            ),
            GitHubAppHeadlessSession.closed_at.is_(None),
        )
    )
    for row in headless_rows:
        result.append(ClosedAppSession(session_id=row.session_id, vault_id=row.vault_id))
    for session_id in ids:
        headless = await session.get(GitHubAppHeadlessSession, session_id)
        if headless is not None:
            continue
        mapping = await session.scalar(
            select(ThreadSession)
            .where(ThreadSession.ma_session_id == session_id)
            .order_by(ThreadSession.created_at.desc())
            .limit(1)
        )
        if mapping is not None and mapping.status == "live":
            continue
        mapped = ThreadSessionRow.model_validate(mapping) if mapping is not None else None
        vault_id = mapped.effective_config.vault_id if mapped and mapped.effective_config else None
        result.append(
            ClosedAppSession(
                session_id=session_id,
                vault_id=vault_id,
            )
        )
    return result
