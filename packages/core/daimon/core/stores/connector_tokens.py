"""Async store for connector_tokens: OAuth connector grants Daimon refreshes itself.

Ciphertext in and out; the Fernet key never reaches the store. A row is
personal (`account_id`) or agent-wide (`account_id` None). Rotation follows
`microsoft_user_tokens`: every write after the first is a compare-and-set on
`updated_at`, so no lock is held while the token endpoint answers. A refresh
first claims the row (`claim_refresh`), which also moves `updated_at`, so a
second refresher sees the claim and waits instead of spending the same
refresh token twice. Watches never read this table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import ConnectorToken
from daimon.core.stores.domain import ConnectorRefreshOwner, ConnectorTokenRow
from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


def _owner(account_id: uuid.UUID | None) -> ColumnElement[bool]:
    if account_id is None:
        return ConnectorToken.account_id.is_(None)
    return ConnectorToken.account_id == account_id


async def upsert_connector_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    server_url: str,
    access_token_encrypted: bytes,
    refresh_token_encrypted: bytes | None,
    token_endpoint: str,
    client_id: str,
    client_secret_encrypted: bytes | None,
    token_endpoint_auth_method: str,
    scope: str | None,
    resource: str | None,
    expires_at: datetime | None,
    refresh_owner: ConnectorRefreshOwner,
    now: datetime,
) -> ConnectorTokenRow:
    """Store a fresh grant (a sign-in or a reconnect), replacing any earlier one."""
    values: dict[str, object] = {
        "access_token_encrypted": access_token_encrypted,
        "refresh_token_encrypted": refresh_token_encrypted,
        "token_endpoint": token_endpoint,
        "client_id": client_id,
        "client_secret_encrypted": client_secret_encrypted,
        "token_endpoint_auth_method": token_endpoint_auth_method,
        "scope": scope,
        "resource": resource,
        "expires_at": expires_at,
        "refresh_owner": refresh_owner,
        "refresh_started_at": None,
        "created_at": now,
        "updated_at": now,
    }
    stmt = insert(ConnectorToken).values(
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        server_url=server_url,
        **values,
    )
    if account_id is None:
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "agent_id", "server_url"],
            index_where=ConnectorToken.account_id.is_(None),
            set_=values,
        )
    else:
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "account_id", "agent_id", "server_url"],
            index_where=ConnectorToken.account_id.is_not(None),
            set_=values,
        )
    orm = (await session.execute(stmt.returning(ConnectorToken))).scalar_one()
    return ConnectorTokenRow.model_validate(orm)


async def get_connector_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    agent_id: uuid.UUID,
    server_url: str,
) -> ConnectorTokenRow | None:
    """One grant: the person's own (`account_id`) or the agent-wide one (None)."""
    orm = await session.scalar(
        select(ConnectorToken).where(
            ConnectorToken.tenant_id == tenant_id,
            _owner(account_id),
            ConnectorToken.agent_id == agent_id,
            ConnectorToken.server_url == server_url,
        )
    )
    return None if orm is None else ConnectorTokenRow.model_validate(orm)


async def get_connector_token_by_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, token_id: uuid.UUID
) -> ConnectorTokenRow | None:
    orm = await session.scalar(
        select(ConnectorToken).where(
            ConnectorToken.id == token_id, ConnectorToken.tenant_id == tenant_id
        )
    )
    return None if orm is None else ConnectorTokenRow.model_validate(orm)


async def list_connector_token_urls(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> frozenset[str]:
    """The server URLs this person holds a Daimon-refreshed token for on this agent."""
    rows = await session.scalars(
        select(ConnectorToken.server_url).where(
            ConnectorToken.tenant_id == tenant_id,
            ConnectorToken.agent_id == agent_id,
            ConnectorToken.account_id == account_id,
            ConnectorToken.refresh_owner == "daimon",
        )
    )
    return frozenset(rows)


async def claim_refresh(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    token_id: uuid.UUID,
    read_at: datetime,
    stale_before: datetime,
    now: datetime,
) -> ConnectorTokenRow | None:
    """Take the one refresh of a Daimon-owned row; None when someone else holds it.

    Wins only over the row as read (`read_at`) with no live claim; a claim older
    than `stale_before` (a refresher that died) can be taken over.
    """
    stmt = (
        update(ConnectorToken)
        .where(
            ConnectorToken.id == token_id,
            ConnectorToken.tenant_id == tenant_id,
            ConnectorToken.refresh_owner == "daimon",
            ConnectorToken.updated_at == read_at,
            or_(
                ConnectorToken.refresh_started_at.is_(None),
                ConnectorToken.refresh_started_at < stale_before,
            ),
        )
        .values(refresh_started_at=now, updated_at=now)
        .returning(ConnectorToken)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else ConnectorTokenRow.model_validate(orm)


async def update_refreshed_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    token_id: uuid.UUID,
    read_at: datetime,
    access_token_encrypted: bytes,
    refresh_token_encrypted: bytes | None,
    scope: str | None,
    expires_at: datetime | None,
    now: datetime,
) -> ConnectorTokenRow | None:
    """Store a rotated token over the claimed row only; releases the claim.

    None when the row changed or went since `read_at` (a reconnect, a
    disconnect): the newer state wins and the rotated token is dropped.
    """
    stmt = (
        update(ConnectorToken)
        .where(
            ConnectorToken.id == token_id,
            ConnectorToken.tenant_id == tenant_id,
            ConnectorToken.updated_at == read_at,
        )
        .values(
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            scope=scope,
            expires_at=expires_at,
            refresh_started_at=None,
            updated_at=now,
        )
        .returning(ConnectorToken)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else ConnectorTokenRow.model_validate(orm)


async def release_refresh(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    token_id: uuid.UUID,
    read_at: datetime,
    now: datetime,
) -> bool:
    """Drop a claim whose refresh failed, leaving the stored tokens as they were."""
    stmt = (
        update(ConnectorToken)
        .where(
            ConnectorToken.id == token_id,
            ConnectorToken.tenant_id == tenant_id,
            ConnectorToken.updated_at == read_at,
        )
        .values(refresh_started_at=None, updated_at=now)
    )
    return cast(CursorResult[Any], await session.execute(stmt)).rowcount > 0


async def delete_connector_token(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    token_id: uuid.UUID,
    read_at: datetime | None = None,
) -> int:
    """Delete one grant; with `read_at`, only if unchanged since. Idempotent."""
    stmt = delete(ConnectorToken).where(
        ConnectorToken.id == token_id, ConnectorToken.tenant_id == tenant_id
    )
    if read_at is not None:
        stmt = stmt.where(ConnectorToken.updated_at == read_at)
    return cast(CursorResult[Any], await session.execute(stmt)).rowcount


async def delete_connector_tokens_for_account(
    session: AsyncSession, *, account_id: uuid.UUID
) -> int:
    """A privacy purge: every personal grant of the account, on every agent."""
    stmt = delete(ConnectorToken).where(ConnectorToken.account_id == account_id)
    return cast(CursorResult[Any], await session.execute(stmt)).rowcount


async def delete_connector_tokens_for_agent(session: AsyncSession, *, agent_id: uuid.UUID) -> int:
    """A deleted agent's grants: no agents table cascades them."""
    stmt = delete(ConnectorToken).where(ConnectorToken.agent_id == agent_id)
    return cast(CursorResult[Any], await session.execute(stmt)).rowcount


async def count_connector_tokens_for_account(
    session: AsyncSession, *, account_id: uuid.UUID
) -> int:
    """How many rows `delete_connector_tokens_for_account` would delete."""
    stmt = (
        select(func.count())
        .select_from(ConnectorToken)
        .where(ConnectorToken.account_id == account_id)
    )
    return int((await session.execute(stmt)).scalar_one())
