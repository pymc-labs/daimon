"""Append/read audit store with narrowly scoped privacy and expiry maintenance.

Callers own transactions. Never pass arguments, messages, tokens or error text.
Normal SQL mutations remain blocked by the migration trigger.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast

from daimon.core._models import Account, SecurityAuditEvent, Tenant
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


class SecurityAuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)
    tenant_id: uuid.UUID
    account_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    platform: str | None
    platform_user_id: str | None
    tool_name: str
    operation: str | None
    outcome: Literal["allowed", "denied", "error"]
    reason: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    token_kind: str | None = None
    """``agent``, ``operator`` or ``cli`` for a registered token; None otherwise."""
    token_jti: uuid.UUID | None = None
    scope: str | None = None
    """The operator-token scope the call was checked against."""


class SecurityAuditRow(SecurityAuditEntry):
    id: uuid.UUID


async def append_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    platform: str | None,
    platform_user_id: str | None,
    tool_name: str,
    operation: str | None,
    outcome: Literal["allowed", "denied", "error"],
    reason: str,
    occurred_at: datetime | None = None,
    token_kind: str | None = None,
    token_jti: uuid.UUID | None = None,
    scope: str | None = None,
) -> SecurityAuditRow | None:
    if occurred_at is not None and occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must include a timezone")
    # Serialize with the sanctioned erasure paths. A queued write arriving after
    # deletion must not recreate tenant rows or the deleted account's identifiers.
    tenant = await session.scalar(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(read=True, key_share=True)
    )
    if tenant is None:
        return None
    if account_id is not None:
        account = await session.scalar(
            select(Account.id)
            .where(Account.id == account_id)
            .with_for_update(read=True, key_share=True)
        )
        if account is None:
            account_id = None
            platform_user_id = None
    event = SecurityAuditEvent(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        platform=platform,
        platform_user_id=platform_user_id,
        tool_name=tool_name,
        operation=operation,
        outcome=outcome,
        reason=reason,
        occurred_at=occurred_at or datetime.now(UTC),
        token_kind=token_kind,
        token_jti=token_jti,
        scope=scope,
    )
    session.add(event)
    await session.flush()
    return SecurityAuditRow.model_validate(event)


async def list_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    since: datetime | None = None,
    account_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[SecurityAuditRow]:
    """Tenant-scoped chronological export, optionally restricted to one account."""
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("limit must be 1..1000 and offset nonnegative")
    if since is not None and since.utcoffset() is None:
        raise ValueError("since must include a timezone")
    stmt = select(SecurityAuditEvent).where(SecurityAuditEvent.tenant_id == tenant_id)
    if since is not None:
        stmt = stmt.where(SecurityAuditEvent.occurred_at >= since)
    if account_id is not None:
        stmt = stmt.where(SecurityAuditEvent.account_id == account_id)
    stmt = (
        stmt.order_by(SecurityAuditEvent.occurred_at, SecurityAuditEvent.id)
        .limit(limit)
        .offset(offset)
    )
    return [SecurityAuditRow.model_validate(row) for row in (await session.scalars(stmt)).all()]


@asynccontextmanager
async def _maintenance(session: AsyncSession) -> AsyncIterator[None]:
    # A savepoint restores the GUC even if the operation fails and PostgreSQL
    # aborts its transaction. Success resets it before returning to the caller.
    async with session.begin_nested():
        await session.execute(text("SET LOCAL daimon.security_audit_maintenance = 'on'"))
        yield
        await session.execute(text("SET LOCAL daimon.security_audit_maintenance = 'off'"))


async def erase_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> int:
    """Remove this account's personal identifiers within one tenant."""
    async with _maintenance(session):
        result = await session.execute(
            update(SecurityAuditEvent)
            .where(
                SecurityAuditEvent.tenant_id == tenant_id,
                SecurityAuditEvent.account_id == account_id,
            )
            .values(account_id=None, platform_user_id=None)
        )
    return cast(CursorResult[Any], result).rowcount


async def erase_account_for_privacy(session: AsyncSession, *, account_id: uuid.UUID) -> None:
    """Account-wide privacy flow: include audit tenants with no remaining principal."""
    await session.scalar(select(Account.id).where(Account.id == account_id).with_for_update())
    tenants = await session.scalars(
        select(SecurityAuditEvent.tenant_id)
        .where(SecurityAuditEvent.account_id == account_id)
        .distinct()
    )
    for tenant_id in tenants:
        await erase_account(session, tenant_id=tenant_id, account_id=account_id)


async def erase_tenant(session: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """Called only by tenant deletion; remove all of its retained audit rows."""
    await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    async with _maintenance(session):
        result = await session.execute(
            delete(SecurityAuditEvent).where(SecurityAuditEvent.tenant_id == tenant_id)
        )
    return cast(CursorResult[Any], result).rowcount


async def prune_events(session: AsyncSession, *, tenant_id: uuid.UUID, older_than: datetime) -> int:
    """Operator retention purge, strictly before the timezone-aware cutoff."""
    if older_than.utcoffset() is None:
        raise ValueError("older_than must include a timezone")
    async with _maintenance(session):
        result = await session.execute(
            delete(SecurityAuditEvent).where(
                SecurityAuditEvent.tenant_id == tenant_id,
                SecurityAuditEvent.occurred_at < older_than,
            )
        )
    return cast(CursorResult[Any], result).rowcount
