"""MCP token registry store — CRUD for the mcp_tokens jti table.

No try/except — exceptions propagate (guideline:architecture).

- create_mcp_token_row: insert a new token row (the mint functions in
  `daimon.core.mcp_auth` call it before signing and supply the jti).
- get_mcp_token: PK lookup by jti; returns McpTokenRow | None.
- revoke_mcp_token: atomic UPDATE…RETURNING that sets revoked_at=now only when
  revoked_at IS NULL; returns McpTokenRow | None (None = already-revoked or unknown).
- list_mcp_tokens: operator listing, live tokens only unless asked.
- lock_mcp_token / add_issued_usd: the per-token promo issuing ceiling.
- update_mcp_token_scopes: replace a live operator token's scopes.

Injected `now` follows guideline:architecture — no datetime.now() calls
inside core logic.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from daimon.core._models import McpToken
from daimon.core.stores.domain import McpTokenKind, McpTokenRow
from sqlalchemy import CursorResult, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_mcp_token_row(
    session: AsyncSession,
    *,
    jti: uuid.UUID,
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: str | None,
    label: str | None,
    created_at: datetime,
    kind: McpTokenKind = "agent",
    scopes: Collection[str] = (),
    expires_at: datetime | None = None,
    max_issued_usd: Decimal | None = None,
) -> None:
    """Insert a new mcp_tokens row.

    The caller (a mint function) generates jti and passes it in so the JWT
    payload and the DB row share the same value. created_at is also
    injected (injected-clock convention per guideline:architecture).
    """
    orm = McpToken(
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        kind=kind,
        scopes=sorted(scopes),
        label=label,
        created_at=created_at,
        expires_at=expires_at,
        max_issued_usd=max_issued_usd,
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


async def list_live_tokens_by_label(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    label: str,
) -> list[McpTokenRow]:
    """Return every live (non-revoked) token row matching `label` in `tenant_id`.

    `tenant_id` is a required, undefaulted filter — not an optional narrowing.
    Two tenants can each publish a report under the same slug on the same
    deployment, so `label` alone is not unique across tenants; a label-only
    lookup would let one tenant's `delete_report` revoke another tenant's
    live token. This is the whole reason the function takes this shape:
    do not remove the tenant filter to "simplify" a caller.

    Returns an empty list rather than raising when nothing matches — "no
    live token with that label" is a legitimate state (a report deleted
    twice, or a report whose token already expired and was cleaned), not
    an error.

    Returns every live match, not just one: nothing constrains labels to be
    unique within a tenant, and a caller revoking "the token for this
    report" should revoke every live one rather than silently picking one.
    """
    stmt = select(McpToken).where(
        McpToken.tenant_id == tenant_id,
        McpToken.label == label,
        McpToken.revoked_at.is_(None),
    )
    result = await session.execute(stmt)
    return [McpTokenRow.model_validate(orm) for orm in result.scalars()]


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


async def list_mcp_tokens(
    session: AsyncSession,
    *,
    now: datetime,
    tenant_id: uuid.UUID | None = None,
    kind: McpTokenKind | None = None,
    include_inactive: bool = False,
) -> list[McpTokenRow]:
    """Token rows, newest first; revoked and expired ones only when `include_inactive`."""
    stmt = select(McpToken).order_by(McpToken.created_at.desc(), McpToken.jti)
    if tenant_id is not None:
        stmt = stmt.where(McpToken.tenant_id == tenant_id)
    if kind is not None:
        stmt = stmt.where(McpToken.kind == kind)
    if not include_inactive:
        stmt = stmt.where(
            McpToken.revoked_at.is_(None),
            or_(McpToken.expires_at.is_(None), McpToken.expires_at > now),
        )
    return [McpTokenRow.model_validate(orm) for orm in (await session.scalars(stmt)).all()]


async def lock_mcp_token(session: AsyncSession, *, jti: uuid.UUID) -> McpTokenRow | None:
    """Row-lock a token so its issuing ceiling is checked and counted serially."""
    stmt = select(McpToken).where(McpToken.jti == jti).with_for_update()
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else McpTokenRow.model_validate(orm)


async def add_issued_usd(session: AsyncSession, *, jti: uuid.UUID, amount_usd: Decimal) -> None:
    """Count promo credit a token issued against its ceiling. Call under `lock_mcp_token`."""
    await session.execute(
        update(McpToken)
        .where(McpToken.jti == jti)
        .values(issued_usd=McpToken.issued_usd + amount_usd)
    )


async def update_mcp_token_scopes(
    session: AsyncSession, *, jti: uuid.UUID, scopes: Collection[str]
) -> McpTokenRow | None:
    """Set a live operator token's scopes; None when no live operator row has `jti`.

    The caller checks the change only removes scopes (`validate_scope_narrowing`)
    under `lock_mcp_token`; the verifier reads the new set on the next request.
    """
    stmt = (
        update(McpToken)
        .where(McpToken.jti == jti, McpToken.kind == "operator", McpToken.revoked_at.is_(None))
        .values(scopes=sorted(scopes))
        .returning(McpToken)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else McpTokenRow.model_validate(orm)
