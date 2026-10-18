"""Durable, encrypted installation-token inventory. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, cast

from cryptography.fernet import MultiFernet
from daimon.core._models import GitHubAppSessionVault, GitHubIssuedToken, ThreadSession
from daimon.core.github_app_auth import group_repository_access
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.session_snapshot import SessionSnapshot, token_scope_parts
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.github_access import list_live_grant_repositories
from daimon.core.stores.github_installation_coverage import installation_covers_repo_sql
from pydantic import BaseModel, ConfigDict
from sqlalchemy import CursorResult, and_, delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

#: A revoked row is kept this long past its `expires_at` as a short forensic
#: trail. GitHub installation tokens live one hour, so the token is long dead.
#: The permanent record is the `security_audit_events` row, which is never pruned.
REVOKED_RETENTION: Final = timedelta(days=7)
#: A `pending` row exists only so the minter can record the token it is about to
#: receive; it never holds a token (`encrypted_token` is null). The row is
#: inserted 55 minutes before `expires_at` and the GitHub call follows at once,
#: so a token minted for it dies one hour after the call, inside this hour; the
#: app JWT the mint uses expires nine minutes after it is built, which bounds how
#: late the call can be. The grace also keeps the row for a minter that is slow
#: to store, though nothing bounds a store's delay. See
#: formal/github_issued_tokens_prune/README.md.
PENDING_GRACE: Final = timedelta(hours=1)


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

    @property
    def chunk(self) -> int | None:
        """Persisted chunk label for a multi-installation inventory row."""
        return self.grant_versions.get("__credential_chunk__")


class GitHubTokenRowClosedError(ValueError):
    """The issued-token row was closed before its minted token could be stored."""


class LiveAppSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    mapping: ThreadSessionRow
    agent_id: uuid.UUID
    expires_at: datetime | None
    has_linked_requester: bool
    permissions_by_repo: dict[int, dict[str, str]]


class LiveMcpAppSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    session_id: str
    tenant_id: uuid.UUID
    vault_id: str
    agent_id: uuid.UUID
    account_id: uuid.UUID | None
    repo_urls: tuple[str, ...]
    repo_resource_ids: dict[str, str]
    expires_at: datetime | None
    permissions_by_repo: dict[int, dict[str, str]]
    last_started_at: datetime


class ClosedAppSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    session_id: str
    vault_id: str | None
    is_mcp: bool = False
    tenant_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


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
    chunk: int | None = None,
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
    if chunk is not None and chunk < 0:
        raise ValueError("token chunk must be non-negative")
    row = GitHubIssuedToken(
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        installation_id=installation_id,
        repo_ids=repo_ids,
        permissions=permissions,
        grant_versions=(
            {**grant_versions, "__credential_chunk__": chunk}
            if chunk is not None
            else grant_versions
        ),
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


async def prune_closed_tokens(session: AsyncSession, *, now: datetime, batch: int) -> int:
    """Delete up to `batch` closed rows; return how many went. The caller commits.

    Closed means `revoked` more than REVOKED_RETENTION past `expires_at`, or
    `pending` (never stored) more than PENDING_GRACE past `expires_at`. `stored`
    and `delivered` rows are never deleted, whatever their age: they are the
    record of a token that may still be live and of the session that holds it.

    One statement selects, locks and deletes. The subquery locks its rows with
    FOR UPDATE SKIP LOCKED, so a row another transaction is storing, revoking or
    delivering is left for the next run, not waited on. A row that committed a
    change after the scan started is rechecked against its new version at the
    lock and drops out. Locked rows cannot change before the DELETE removes them,
    so there is no stale selection to guard against. MATERIALIZED makes the
    planner run the locking subquery exactly once. Oldest rows go first so a
    backlog drains in order.
    """
    if batch <= 0:
        raise ValueError("batch must be positive")
    closed = or_(
        and_(
            GitHubIssuedToken.status == "revoked",
            GitHubIssuedToken.expires_at < now - REVOKED_RETENTION,
        ),
        and_(
            GitHubIssuedToken.status == "pending",
            GitHubIssuedToken.expires_at < now - PENDING_GRACE,
        ),
    )
    victims = (
        select(GitHubIssuedToken.token_id)
        .where(closed)
        .order_by(GitHubIssuedToken.expires_at, GitHubIssuedToken.token_id)
        .limit(batch)
        .with_for_update(skip_locked=True)
        .cte("victims")
        .prefix_with("MATERIALIZED")
    )
    result = await session.execute(
        delete(GitHubIssuedToken).where(GitHubIssuedToken.token_id.in_(select(victims.c.token_id)))
    )
    return cast(CursorResult[object], result).rowcount or 0


async def record_revoke_attempt(session: AsyncSession, *, token_id: uuid.UUID) -> None:
    row = await session.get(GitHubIssuedToken, token_id, with_for_update=True)
    if row is None:
        raise ValueError("unknown token")
    row.revoke_attempts += 1
    await session.flush()


def decrypt_issued_token(row: IssuedToken, *, fernet: MultiFernet) -> str | None:
    return decrypt_token(fernet, row.encrypted_token) if row.encrypted_token is not None else None


async def select_stale_tokens(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    include_live_version_bumps: bool = False,
    token_id: uuid.UUID | None = None,
) -> list[IssuedToken]:
    """Find live tokens with changed grant or authorization state.

    A version bump is reported only when the caller asks to rotate or to
    verify a newly minted token. The sweep never revokes for a version bump,
    including a provisional stored token that MA may already hold. A hard
    change revokes early: installation, grant, or authorization removed,
    staged, or inactive, or write access narrowed.
    """
    current = now or datetime.now(UTC)
    token_filter = "AND token.token_id = :token_id" if token_id is not None else ""
    coverage_sql = installation_covers_repo_sql("auth_row.repo_full_name", "installation")
    statement = (
        select(GitHubIssuedToken)
        .from_statement(
            text(
                f"""
            SELECT token.*
            FROM github_issued_tokens AS token
            LEFT JOIN github_app_installations AS installation
              ON installation.installation_id = token.installation_id
            WHERE token.status IN ('stored', 'delivered')
              AND token.expires_at > :now
              {token_filter}
              AND (
                token.revoke_after IS NULL
                OR token.revoke_after <= :now
                OR token.superseded_at IS NOT NULL
              )
              AND (
                installation.installation_id IS NULL
                OR installation.suspended_at IS NOT NULL
                OR installation.github_removed_at IS NOT NULL
                OR EXISTS (
                  SELECT 1
                  FROM unnest(token.repo_ids) AS repo(repo_id)
                  LEFT JOIN agent_github_grants AS grant_row
                    ON grant_row.tenant_id = token.tenant_id
                   AND grant_row.agent_id = token.agent_id
                   AND grant_row.repo_id = repo.repo_id
                   AND grant_row.app_visible
                  -- The row the agent may use: its own, else the server-wide one.
                  LEFT JOIN LATERAL (
                    SELECT candidate.*
                    FROM tenant_github_repos AS candidate
                    WHERE candidate.tenant_id = token.tenant_id
                      AND candidate.repo_id = repo.repo_id
                      AND (
                        candidate.scope_agent_id IS NULL
                        OR candidate.scope_agent_id = token.agent_id
                      )
                    ORDER BY candidate.scope_agent_id IS NULL
                    LIMIT 1
                  ) AS auth_row ON true
                  WHERE grant_row.repo_id IS NULL
                     OR auth_row.repo_id IS NULL
                     OR auth_row.status <> 'active'
                     OR auth_row.installation_id <> token.installation_id
                     OR NOT ({coverage_sql})
                     OR (
                       token.superseded_at IS NULL
                       AND :include_live_version_bumps
                       AND (
                         token.grant_versions ->> ('grant:' || repo.repo_id::text)
                           IS DISTINCT FROM grant_row.version::text
                         OR token.grant_versions ->> ('authorization:' || repo.repo_id::text)
                           IS DISTINCT FROM auth_row.version::text
                       )
                     )
                     OR (
                       token.permissions ->> 'contents' = 'write'
                       AND grant_row.ceiling_access <> 'write'
                     )
                )
              )
            """
            )
        )
        .execution_options(populate_existing=True)
    )
    rows = await session.scalars(
        statement,
        {
            "now": current,
            "include_live_version_bumps": include_live_version_bumps,
            "token_id": token_id,
        },
    )
    return [IssuedToken.model_validate(row) for row in rows]


async def session_token_needs_rotation(session: AsyncSession, *, session_id: str) -> bool:
    """Detect a revoked, missing, or changed current token before a live turn runs."""
    tokens = await list_session_tokens(session, session_id=session_id)
    now = datetime.now(UTC)
    current = [row for row in tokens if row.superseded_at is None and row.expires_at > now]
    if not current:
        return True
    return bool(await session_installations_needing_rotation(session, session_id=session_id))


async def session_installations_needing_rotation(
    session: AsyncSession, *, session_id: str
) -> tuple[int, ...]:
    """Return only installations whose current token is stale or undelivered."""
    tokens = await list_session_tokens(session, session_id=session_id)
    now = datetime.now(UTC)
    current = [row for row in tokens if row.superseded_at is None and row.expires_at > now]
    mapped = await session.scalar(
        select(ThreadSession)
        .where(ThreadSession.ma_session_id == session_id, ThreadSession.status == "live")
        .limit(1)
    )
    snapshot = (
        SessionSnapshot.model_validate(mapped.effective_config)
        if mapped is not None and mapped.effective_config is not None
        else None
    )
    excluded_urls: set[str] = (
        {
            url
            for url, since in snapshot.unavailable_repos
            if since.tzinfo is not None
            and since.utcoffset() is not None
            and now - since < timedelta(minutes=60)
        }
        if snapshot is not None
        else set()
    )
    session_token = await session.scalar(
        select(GitHubIssuedToken)
        .where(GitHubIssuedToken.session_id == session_id)
        .order_by(GitHubIssuedToken.created_at)
        .limit(1)
    )
    live_grants = (
        await list_live_grant_repositories(
            session,
            tenant_id=session_token.tenant_id,
            agent_id=session_token.agent_id,
        )
        if session_token is not None
        else []
    )
    multi_installation = len({repo.installation_id for _, repo in live_grants}) > 1
    desired_groups = group_repository_access(
        [
            (
                repo.installation_id,
                repo.repo_id,
                "write"
                if grant.ceiling_access == "write" and repo.max_access == "write"
                else "read",
            )
            for grant, repo in live_grants
            if f"https://github.com/{repo.repo_full_name}" not in excluded_urls
        ],
        sort_repo_ids=multi_installation,
    )
    if not multi_installation and not (snapshot and snapshot.token_scopes):
        # Legacy single-installation sessions use main's status and grant-version
        # checks. A refused group must not renumber or refresh a healthy token.
        differing_installations: set[int] = set()
    elif multi_installation:
        desired_groups.sort(key=lambda group: (group[0], group[1], group[2]))
        chunks: dict[int, int] = {}
        desired: set[tuple[int, int, tuple[int, ...]]] = set()
        for installation_id, _, repo_ids in desired_groups:
            chunk = chunks.get(installation_id, 0)
            chunks[installation_id] = chunk + 1
            desired.add((installation_id, chunk, tuple(sorted(repo_ids))))
        live: set[tuple[int, int | None, tuple[int, ...]]] = {
            (row.installation_id, row.chunk, tuple(sorted(row.repo_ids))) for row in current
        }
        differing_installations = {
            installation_id for installation_id, _, _ in live.symmetric_difference(desired)
        }
        if len(live) != len(current):
            differing_installations.update(row.installation_id for row in current)
        if snapshot is not None:
            recorded = {
                (installation_id, chunk, tuple(sorted(repo_ids)))
                for scope in snapshot.token_scopes
                for installation_id, chunk, repo_ids, _, _ in (token_scope_parts(scope),)
                if chunk is not None
            }
            differing_installations.update(
                installation_id for installation_id, _, _ in recorded - desired
            )
    else:
        # A saved multi-installation scope and one desired installation are a
        # mode transition. The caller refreshes all surviving groups into the
        # legacy single-installation shape, regardless of sweep ordering.
        differing_installations = (
            {scope[0] for scope in snapshot.token_scopes}
            - {installation_id for installation_id, _, _ in desired_groups}
            if snapshot
            else set()
        )
    stale_ids = {
        row.token_id for row in await select_stale_tokens(session, include_live_version_bumps=True)
    }
    return tuple(
        sorted(
            differing_installations
            | {
                row.installation_id
                for row in current
                if row.status != "delivered"
                or row.link_generation is not None
                or row.token_id in stale_ids
            }
        )
    )


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
    """Let equal-access tokens expire; revoke narrowed access on the next sweep.

    MA updates resources during a turn, but a running tool keeps its old vault
    environment until that tool ends. The old token therefore remains usable
    through its natural expiry when the replacement grants the same access.
    Tokens superseded by an earlier rotation are checked against this
    replacement too, so a later narrowing revokes them now rather than at
    expiry.
    """
    rows = list(
        await session.scalars(
            select(GitHubIssuedToken).where(
                GitHubIssuedToken.session_id == session_id,
                GitHubIssuedToken.status == "delivered",
            )
        )
    )
    replacements = {
        repo_id: row.permissions
        for row in rows
        if row.token_id in except_ids
        for repo_id in row.repo_ids
    }
    rank = {"none": 0, "read": 1, "write": 2}
    for row in rows:
        if row.token_id in except_ids:
            continue
        narrowed = any(
            rank.get(replacements.get(repo_id, {}).get(key, "none"), 0) < rank.get(value, 0)
            for repo_id in row.repo_ids
            for key, value in row.permissions.items()
        )
        if row.superseded_at is not None:
            if narrowed and (row.revoke_after is None or row.revoke_after > now):
                row.revoke_after = now
            continue
        row.superseded_at = now
        # Inventory expires five minutes early; GitHub tokens live for an hour.
        # Keep a minute of clock/HTTP margin before local cleanup.
        row.revoke_after = now if narrowed else row.expires_at + timedelta(minutes=6)
    await session.flush()


async def keep_failed_rotation_tokens(
    session: AsyncSession, *, session_id: str, token_ids: frozenset[uuid.UUID], now: datetime
) -> frozenset[uuid.UUID]:
    """Keep new tokens a failed active-turn refresh already handed to MA.

    A running tool may hold one, so each is recorded as a superseded token of
    the session and left to expire like any other. Current grants determine
    whether it may stay; the old session token may have less access than the
    new grant. Returns the kept ids.
    """
    if not token_ids:
        return frozenset()
    rows = list(
        await session.scalars(
            select(GitHubIssuedToken).where(
                GitHubIssuedToken.token_id.in_(token_ids),
                GitHubIssuedToken.status.in_(("stored", "delivered")),
            )
        )
    )
    kept: set[uuid.UUID] = set()
    hard_stale = {row.token_id for row in await select_stale_tokens(session)}
    for row in rows:
        if row.token_id in hard_stale:
            continue
        row.session_id = session_id
        row.status = "delivered"
        row.superseded_at = now
        row.revoke_after = row.expires_at + timedelta(minutes=6)
        kept.add(row.token_id)
    await session.flush()
    return frozenset(kept)


async def restore_session_tokens(session: AsyncSession, *, token_ids: frozenset[uuid.UUID]) -> None:
    """Undo a failed rotation's pending revocation of the old tokens."""
    if not token_ids:
        return
    await session.execute(
        update(GitHubIssuedToken)
        .where(
            GitHubIssuedToken.token_id.in_(token_ids),
            GitHubIssuedToken.status == "delivered",
        )
        .values(superseded_at=None, revoke_after=None)
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
    """Retire provisional tokens only after GitHub expiry.

    A process can crash after MA receives a token but before the provisional
    row is marked delivered. Revoking a recent stored row would break that
    live credential.
    """
    current = now or datetime.now(UTC)
    rows = await session.scalars(
        select(GitHubIssuedToken).where(
            GitHubIssuedToken.status == "stored",
            GitHubIssuedToken.session_id.startswith("pending:"),
            GitHubIssuedToken.expires_at <= current - timedelta(minutes=6),
        )
    )
    return [IssuedToken.model_validate(row) for row in rows]


async def register_headless_app_session(
    session: AsyncSession, *, session_id: str, tenant_id: uuid.UUID, vault_id: str
) -> None:
    await register_app_session_vault(
        session, session_id=session_id, tenant_id=tenant_id, vault_id=vault_id, is_unmapped=True
    )


async def register_app_session_vault(
    session: AsyncSession,
    *,
    session_id: str,
    tenant_id: uuid.UUID,
    vault_id: str,
    is_unmapped: bool = False,
    is_mcp: bool = False,
    agent_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    repo_urls: tuple[str, ...] = (),
    repo_resource_ids: dict[str, str] | None = None,
) -> None:
    if is_mcp and (not is_unmapped or agent_id is None or account_id is None):
        raise ValueError("MCP app vault requires an unmapped session, agent and requester")
    session.add(
        GitHubAppSessionVault(
            session_id=session_id,
            tenant_id=tenant_id,
            vault_id=vault_id,
            is_unmapped=is_unmapped,
            is_mcp=is_mcp,
            agent_id=agent_id,
            account_id=account_id,
            repo_urls=list(repo_urls) if is_mcp else None,
            repo_resource_ids=repo_resource_ids if is_mcp else None,
        )
    )
    await session.flush()


async def finish_headless_app_session(session: AsyncSession, *, session_id: str) -> str | None:
    row = await session.get(GitHubAppSessionVault, session_id, with_for_update=True)
    if row is None or row.closed_at is not None:
        return None
    if row.finished_at is None:
        row.finished_at = datetime.now(UTC)
    await session.flush()
    return row.vault_id


async def touch_unmapped_app_session(session: AsyncSession, *, session_id: str) -> bool | None:
    """Extend an open MCP vault. False means closed or past its turn ceiling."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(GitHubAppSessionVault)
        .where(
            GitHubAppSessionVault.session_id == session_id,
            GitHubAppSessionVault.is_unmapped.is_(True),
            GitHubAppSessionVault.closed_at.is_(None),
            GitHubAppSessionVault.finished_at.is_(None),
            GitHubAppSessionVault.last_started_at > now - timedelta(minutes=46),
        )
        .values(last_started_at=now)
        .returning(GitHubAppSessionVault.session_id)
    )
    if result.scalar_one_or_none() is not None:
        return True
    is_unmapped = await session.scalar(
        select(GitHubAppSessionVault.is_unmapped).where(
            GitHubAppSessionVault.session_id == session_id
        )
    )
    return False if is_unmapped else None


async def touch_running_mcp_app_session(
    session: AsyncSession, *, session_id: str, now: datetime
) -> None:
    """Keep a session observed running by MA out of the idle vault close sweep."""
    await session.execute(
        update(GitHubAppSessionVault)
        .where(
            GitHubAppSessionVault.session_id == session_id,
            GitHubAppSessionVault.is_mcp.is_(True),
            GitHubAppSessionVault.closed_at.is_(None),
            GitHubAppSessionVault.finished_at.is_(None),
        )
        .values(last_started_at=now)
    )


async def mark_headless_app_session_closed(session: AsyncSession, *, session_id: str) -> None:
    row = await session.get(GitHubAppSessionVault, session_id, with_for_update=True)
    if row is not None and row.closed_at is None:
        row.closed_at = datetime.now(UTC)
        await session.flush()


async def recheck_token_revoke_due(
    session: AsyncSession, *, token_id: uuid.UUID, now: datetime
) -> tuple[IssuedToken, bool] | None:
    """Recheck a sweep candidate under its row lock before GitHub DELETE.

    The caller keeps this transaction open until DELETE and the inventory
    update finish, so rotation cannot restore a token selected by an old sweep.
    """
    row = await session.get(GitHubIssuedToken, token_id, with_for_update=True)
    if row is None or row.status not in ("stored", "delivered"):
        return None
    hard_stale = bool(await select_stale_tokens(session, now=now, token_id=token_id))
    due = row.status == "delivered" and row.revoke_after is not None and row.revoke_after <= now
    abandoned = (
        row.status == "stored"
        and row.session_id.startswith("pending:")
        and row.expires_at <= now - timedelta(minutes=6)
    )
    if not (hard_stale or due or abandoned):
        return None
    naturally_expired = not hard_stale and (
        abandoned or (due and row.revoke_after is not None and row.revoke_after >= row.expires_at)
    )
    return IssuedToken.model_validate(row), naturally_expired


async def list_live_app_sessions(
    session: AsyncSession, *, session_id: str | None = None
) -> list[LiveAppSession]:
    statement = (
        select(ThreadSession, GitHubIssuedToken)
        .outerjoin(
            GitHubIssuedToken,
            (GitHubIssuedToken.session_id == ThreadSession.ma_session_id)
            & (GitHubIssuedToken.status == "delivered")
            & (GitHubIssuedToken.superseded_at.is_(None)),
        )
        .where(ThreadSession.status == "live")
    )
    if session_id is not None:
        statement = statement.where(ThreadSession.ma_session_id == session_id)
    rows = await session.execute(statement)
    grouped: dict[str, LiveAppSession] = {}
    for mapping, token in rows:
        mapped = ThreadSessionRow.model_validate(mapping)
        if mapped.effective_config is None or mapped.effective_config.github_mode != "app":
            continue
        ma_agent_id = mapped.effective_config.ma_agent_id or mapped.ma_agent_id
        agent_id = (
            derive_agent_uuid(tenant_id=mapped.tenant_id, ma_agent_id=ma_agent_id)
            if ma_agent_id
            else token.agent_id
            if token is not None
            else None
        )
        if agent_id is None:
            continue
        current = grouped.get(mapping.ma_session_id)
        if current is None:
            grouped[mapping.ma_session_id] = LiveAppSession(
                mapping=mapped,
                agent_id=agent_id,
                expires_at=token.expires_at if token is not None else None,
                has_linked_requester=token is not None and token.link_generation is not None,
                permissions_by_repo=(
                    {repo_id: token.permissions for repo_id in token.repo_ids}
                    if token is not None
                    else {}
                ),
            )
        elif token is not None:
            grouped[mapping.ma_session_id] = current.model_copy(
                update={
                    "expires_at": (
                        min(current.expires_at, token.expires_at)
                        if current.expires_at is not None
                        else token.expires_at
                    ),
                    "has_linked_requester": current.has_linked_requester
                    or token.link_generation is not None,
                    "permissions_by_repo": {
                        **current.permissions_by_repo,
                        **{repo_id: token.permissions for repo_id in token.repo_ids},
                    },
                }
            )
    return list(grouped.values())


async def list_live_mcp_app_sessions(
    session: AsyncSession,
    *,
    now: datetime,
    session_id: str | None = None,
    include_expired: bool = False,
) -> list[LiveMcpAppSession]:
    statement = select(GitHubAppSessionVault).where(
        GitHubAppSessionVault.is_mcp.is_(True),
        GitHubAppSessionVault.closed_at.is_(None),
        GitHubAppSessionVault.finished_at.is_(None),
    )
    if not include_expired:
        statement = statement.where(
            GitHubAppSessionVault.last_started_at > now - timedelta(minutes=46)
        )
    if session_id is not None:
        statement = statement.where(GitHubAppSessionVault.session_id == session_id)
    vaults = await session.scalars(statement)
    result: list[LiveMcpAppSession] = []
    for vault in vaults:
        if vault.agent_id is None:
            continue
        tokens = await session.scalars(
            select(GitHubIssuedToken).where(
                GitHubIssuedToken.session_id == vault.session_id,
                GitHubIssuedToken.status == "delivered",
                GitHubIssuedToken.superseded_at.is_(None),
            )
        )
        token_rows = list(tokens)
        result.append(
            LiveMcpAppSession(
                session_id=vault.session_id,
                tenant_id=vault.tenant_id,
                vault_id=vault.vault_id,
                agent_id=vault.agent_id,
                account_id=vault.account_id,
                last_started_at=vault.last_started_at,
                repo_urls=tuple(vault.repo_urls or ()),
                repo_resource_ids=vault.repo_resource_ids or {},
                expires_at=min((row.expires_at for row in token_rows), default=None),
                permissions_by_repo={
                    repo_id: row.permissions for row in token_rows for repo_id in row.repo_ids
                },
            )
        )
    return result


async def closed_app_session_for_id(
    session: AsyncSession, *, session_id: str, now: datetime
) -> ClosedAppSession | None:
    row = await session.get(GitHubAppSessionVault, session_id)
    if row is None or row.closed_at is not None:
        return None
    if row.is_unmapped:
        if row.finished_at is None and row.last_started_at > now - timedelta(minutes=46):
            return None
    else:
        mapping = await session.scalar(
            select(ThreadSession)
            .where(ThreadSession.ma_session_id == row.session_id)
            .order_by(ThreadSession.created_at.desc())
            .limit(1)
        )
        if mapping is not None and mapping.status == "live":
            return None
        if mapping is None and row.created_at > now - timedelta(minutes=1):
            return None
    return ClosedAppSession(
        session_id=row.session_id,
        vault_id=row.vault_id,
        is_mcp=row.is_mcp,
        tenant_id=row.tenant_id,
        account_id=row.account_id,
    )


async def list_closed_app_sessions(
    session: AsyncSession, *, now: datetime
) -> list[ClosedAppSession]:
    """App vaults whose headless run or mapped session ended."""
    result: list[ClosedAppSession] = []
    vaults = await session.scalars(
        select(GitHubAppSessionVault).where(GitHubAppSessionVault.closed_at.is_(None))
    )
    for row in vaults:
        closed = await closed_app_session_for_id(session, session_id=row.session_id, now=now)
        if closed is not None:
            result.append(closed)
    return result
