"""MCP token registry store — CRUD for the mcp_tokens jti table.

Three functions, no try/except — exceptions propagate (guideline:architecture).

- create_mcp_token_row: insert a new token row (called by mint_agent_mcp_token
  before signing; mint supplies jti so no refresh needed).
- get_mcp_token: PK lookup by jti; returns McpTokenRow | None.
- revoke_mcp_token: atomic UPDATE…RETURNING that sets revoked_at=now only when
  revoked_at IS NULL; returns McpTokenRow | None (None = already-revoked or unknown).

Injected `now` in revoke_mcp_token follows guideline:architecture — no
datetime.now() calls inside core logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import McpToken
from daimon.core.stores.domain import McpTokenRow
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_mcp_token_row(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: str,
    label: str | None,
    created_at: datetime,
) -> None:
    """Insert a new mcp_tokens row.

    The caller (mint_agent_mcp_token) generates jti and passes it in so
    the JWT payload and the DB row share the same value. created_at is also
    injected (injected-clock convention per guideline:architecture).
    """
    orm = McpToken(
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        label=label,
        created_at=created_at,
    )
    session.add(orm)
    await session.flush()


async def get_mcp_token(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
) -> McpTokenRow | None:
    """Return the McpTokenRow for `jti`, or None if not found.

    Does NOT filter revoked_at — the caller decides whether to reject
    revoked tokens (the verifier checks row.revoked_at is not None).
    """
    orm = await session.get(McpToken, jti)
    if orm is None:
        return None
    return McpTokenRow.model_validate(orm)


async def revoke_mcp_token(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
    now: datetime,
) -> McpTokenRow | None:
    """Atomically set revoked_at=now on a live token row.

    Returns the updated McpTokenRow when the token was live and is now
    revoked. Returns None when the token is already revoked or the jti is
    unknown — both are no-ops (idempotent by design).

    The WHERE revoked_at IS NULL guard makes double-revoke safe without
    any application-level locking.
    """
    stmt = (
        update(McpToken)
        .where(McpToken.jti == jti, McpToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .returning(McpToken)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        return None
    return McpTokenRow.model_validate(orm)


async def delete_tokens_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> int:
    """Hard-delete every mcp_tokens row for an account. Idempotent.

    Returns rows deleted; never raises on 0. Used by the GDPR purge
    orchestrator before delete_account — this is the crash fix.

    NOT revoke_mcp_token: that soft-revoke only sets revoked_at and leaves
    the row, so it still trips the account_id FK when the account is deleted.
    jti is the PK and account_id is non-unique, so one account may own many
    token rows — rowcount is 0..N.
    """
    result = await session.execute(delete(McpToken).where(McpToken.account_id == account_id))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_tokens_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> int:
    """Read-only count of mcp_tokens rows for an account.

    Used by the purge preview twin (privacy.py) to show what
    delete_tokens_for_account would remove. Never mutates.
    """
    result = await session.execute(
        select(func.count()).select_from(McpToken).where(McpToken.account_id == account_id)
    )
    return result.scalar_one()
