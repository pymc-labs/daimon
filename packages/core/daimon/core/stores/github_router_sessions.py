"""Router session records: the one authority for a session's GitHub MCP router bearer.

A record binds an opaque bearer to a tenant, agent, optional account and,
once known, the Managed Agents session. Only the bearer's SHA-256 is stored.
`close_local` is the revocation: it runs before any remote cleanup, so a
failed cleanup never leaves the bearer usable. Callers own transactions.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from daimon.core._models import (
    GitHubAppSessionVault,
    GitHubIssuedToken,
    GitHubRouterSession,
    ThreadSession,
)
from pydantic import BaseModel, ConfigDict
from sqlalchemy import CursorResult, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

#: How long a router session record stays valid without renewal. Each refresh
#: point (a turn's vault refresh, a headless turn start, a token rotation) pushes
#: it forward, so it only lapses for a session nobody uses. Longer than the
#: forty-five-minute turn ceiling, so one turn never outlives its record.
GITHUB_ROUTER_SESSION_TTL: Final = timedelta(hours=2)
#: `touch_last_used` writes at most once per this interval per record, so a
#: busy session does not write a row on every MCP request.
TOUCH_INTERVAL: Final = timedelta(minutes=1)


class GitHubRouterSessionRecord(BaseModel):
    """A router session row without its credential hash."""

    model_config = ConfigDict(from_attributes=True, frozen=True)
    id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    account_id: uuid.UUID | None
    ma_session_id: str | None
    created_at: datetime
    expires_at: datetime
    closed_at: datetime | None
    last_used_at: datetime | None


def credential_sha256(bearer: str) -> bytes:
    return hashlib.sha256(bearer.encode()).digest()


def _require_aware(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


async def create(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    expires_at: datetime,
    ma_session_id: str | None = None,
) -> tuple[GitHubRouterSessionRecord, str]:
    """Create a record and return it with its plaintext bearer.

    The bearer is shown once, to be written to the session's vault; it is
    never stored or logged.
    """
    _require_aware(expires_at, "expires_at")
    bearer = secrets.token_urlsafe(32)
    row = GitHubRouterSession(
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=account_id,
        ma_session_id=ma_session_id,
        credential_sha256=credential_sha256(bearer),
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return GitHubRouterSessionRecord.model_validate(row), bearer


async def verify(
    session: AsyncSession, *, bearer: str, now: datetime | None = None
) -> GitHubRouterSessionRecord | None:
    """The open, unexpired record for a bearer, else None.

    This does not check that the Managed Agents session is still live; the
    caller does that with `is_session_live`.
    """
    if not bearer:
        return None
    current = now or datetime.now(UTC)
    row = await session.scalar(
        select(GitHubRouterSession).where(
            GitHubRouterSession.credential_sha256 == credential_sha256(bearer),
            GitHubRouterSession.closed_at.is_(None),
            GitHubRouterSession.expires_at > current,
        )
    )
    return GitHubRouterSessionRecord.model_validate(row) if row is not None else None


async def get(
    session: AsyncSession, *, router_session_id: uuid.UUID
) -> GitHubRouterSessionRecord | None:
    row = await session.get(GitHubRouterSession, router_session_id)
    return GitHubRouterSessionRecord.model_validate(row) if row is not None else None


async def is_session_live(
    session: AsyncSession,
    record: GitHubRouterSessionRecord,
    *,
    now: datetime | None = None,  # accepted for callers; liveness does not depend on age
) -> bool:
    """True while the record's Managed Agents session is still running.

    A chat session is live while its `thread_sessions` row is `live`. A
    headless or MCP-origin session has no such row; it is live while its
    unmapped app vault is neither closed nor finished. Turn age is not
    checked: one turn may run for hours while the scheduler keeps rotating
    its tokens, and the idle sweep finishes and closes the vault (closing
    the router record first) when the session is really done. A record with
    no session id yet is not live: nothing can call the router before the
    session exists.
    """
    if record.ma_session_id is None:
        return False
    mapped_live = exists().where(
        ThreadSession.ma_session_id == record.ma_session_id,
        ThreadSession.tenant_id == record.tenant_id,
        ThreadSession.status == "live",
    )
    unmapped_live = exists().where(
        GitHubAppSessionVault.session_id == record.ma_session_id,
        GitHubAppSessionVault.tenant_id == record.tenant_id,
        GitHubAppSessionVault.is_unmapped.is_(True),
        GitHubAppSessionVault.closed_at.is_(None),
        GitHubAppSessionVault.finished_at.is_(None),
    )
    return bool(await session.scalar(select(or_(mapped_live, unmapped_live))))


async def set_ma_session_id(
    session: AsyncSession, *, router_session_id: uuid.UUID, ma_session_id: str
) -> None:
    """Bind the record to its Managed Agents session once, never to another one."""
    row = await session.get(GitHubRouterSession, router_session_id, with_for_update=True)
    if row is None:
        raise ValueError("unknown router session")
    if row.ma_session_id is not None and row.ma_session_id != ma_session_id:
        raise ValueError("router session is already bound to another session")
    row.ma_session_id = ma_session_id
    await session.flush()


async def renew(
    session: AsyncSession, *, router_session_id: uuid.UUID, expires_at: datetime
) -> bool:
    """Extend an open record's expiry; never shorten it. False when closed or unknown."""
    _require_aware(expires_at, "expires_at")
    row = await session.get(GitHubRouterSession, router_session_id, with_for_update=True)
    if row is None or row.closed_at is not None:
        return False
    if expires_at > row.expires_at:
        row.expires_at = expires_at
        await session.flush()
    return True


async def close_local(
    session: AsyncSession, *, router_session_id: uuid.UUID, now: datetime | None = None
) -> None:
    """Revoke the bearer locally. Idempotent: a closed record keeps its first close time."""
    await session.execute(
        update(GitHubRouterSession)
        .where(
            GitHubRouterSession.id == router_session_id,
            GitHubRouterSession.closed_at.is_(None),
        )
        .values(closed_at=now or datetime.now(UTC))
    )


async def close_for_ma_session(
    session: AsyncSession, *, ma_session_id: str, now: datetime | None = None
) -> int:
    """Close every open record of a Managed Agents session; return how many closed."""
    result = await session.execute(
        update(GitHubRouterSession)
        .where(
            GitHubRouterSession.ma_session_id == ma_session_id,
            GitHubRouterSession.closed_at.is_(None),
        )
        .values(closed_at=now or datetime.now(UTC))
    )
    return cast(CursorResult[object], result).rowcount or 0


async def close_for_account(
    session: AsyncSession, *, account_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Close an erased account's open records before ON DELETE SET NULL detaches them."""
    result = await session.execute(
        update(GitHubRouterSession)
        .where(
            GitHubRouterSession.account_id == account_id,
            GitHubRouterSession.closed_at.is_(None),
        )
        .values(closed_at=now or datetime.now(UTC))
    )
    return cast(CursorResult[object], result).rowcount or 0


async def touch_last_used(
    session: AsyncSession, *, router_session_id: uuid.UUID, now: datetime | None = None
) -> bool:
    """Record use at most once per TOUCH_INTERVAL. True when this call wrote."""
    current = now or datetime.now(UTC)
    result = await session.execute(
        update(GitHubRouterSession)
        .where(
            GitHubRouterSession.id == router_session_id,
            or_(
                GitHubRouterSession.last_used_at.is_(None),
                GitHubRouterSession.last_used_at <= current - TOUCH_INTERVAL,
            ),
        )
        .values(last_used_at=current)
    )
    return bool(cast(CursorResult[object], result).rowcount)


async def router_session_id_for_ma_session(
    session: AsyncSession, *, ma_session_id: str
) -> uuid.UUID | None:
    """The router session a Managed Agents session's new tokens must carry.

    The open record bound to the session wins. A session whose record was
    bound late is found through its current token rows instead, so a
    rotation never drops the stamp.
    """
    record_id = await session.scalar(
        select(GitHubRouterSession.id)
        .where(
            GitHubRouterSession.ma_session_id == ma_session_id,
            GitHubRouterSession.closed_at.is_(None),
        )
        .order_by(GitHubRouterSession.created_at.desc())
        .limit(1)
    )
    if record_id is not None:
        return record_id
    return await session.scalar(
        select(GitHubIssuedToken.router_session_id)
        .where(
            GitHubIssuedToken.session_id == ma_session_id,
            GitHubIssuedToken.router_session_id.is_not(None),
            GitHubIssuedToken.status.in_(("stored", "delivered")),
        )
        .order_by(GitHubIssuedToken.created_at.desc())
        .limit(1)
    )
