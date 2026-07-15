"""Integration tests for mcp_tokens store — real Postgres.

Covers the four store behaviors for PHASE-77-TOKEN-01:
1. create_mcp_token_row inserts a row readable by get_mcp_token.
2. get_mcp_token returns an McpTokenRow (Pydantic, never ORM).
3. revoke_mcp_token flips revoked_at from None to the injected now.
4. revoke_mcp_token on an already-revoked or unknown jti returns None (no-op).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core._models import Account, Tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import mcp_tokens as store
from daimon.core.stores.domain import McpTokenRow
from sqlalchemy.ext.asyncio import AsyncSession


async def _seed_tenant_and_account(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Tenant + Account pair; return (tenant_id, account_id)."""
    guild_id = str(uuid.uuid4())
    tenant = Tenant(
        id=derive_tenant_uuid(platform="discord", workspace_id=guild_id),
        platform="discord",
        external_id=guild_id,
    )
    session.add(tenant)
    await session.flush()

    account = Account(tenant_id=tenant.id)
    session.add(account)
    await session.flush()
    await session.refresh(account)

    return tenant.id, account.id


async def test_create_mcp_token_row_inserts_row_readable_by_get(
    db_session: AsyncSession,
) -> None:
    """create_mcp_token_row writes a row that get_mcp_token can retrieve by jti."""
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    jti = uuid.uuid4()
    agent_id_str = str(uuid.uuid4())
    now = datetime.now(tz=UTC)

    await store.create_mcp_token_row(
        db_session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=agent_id_str,
        label="test-label",
        created_at=now,
    )

    row = await store.get_mcp_token(db_session, jti=jti)
    assert row is not None, "get_mcp_token must return the row that was just created"
    assert row.jti == jti, "jti must round-trip through the store"
    assert row.account_id == account_id, "account_id must round-trip through the store"
    assert row.tenant_id == tenant_id, "tenant_id must round-trip through the store"
    assert row.agent_id == agent_id_str, "agent_id must round-trip through the store"
    assert row.label == "test-label", "label must round-trip through the store"
    assert row.revoked_at is None, "freshly created row must have revoked_at = None"


async def test_get_mcp_token_returns_pydantic_row_not_orm(
    db_session: AsyncSession,
) -> None:
    """get_mcp_token must return an McpTokenRow (Pydantic), never the ORM instance."""
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    jti = uuid.uuid4()
    now = datetime.now(tz=UTC)

    await store.create_mcp_token_row(
        db_session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=str(uuid.uuid4()),
        label=None,
        created_at=now,
    )

    row = await store.get_mcp_token(db_session, jti=jti)
    assert isinstance(row, McpTokenRow), (
        "get_mcp_token must return McpTokenRow (Pydantic), not the ORM instance"
    )


async def test_revoke_mcp_token_flips_revoked_at_from_none_to_now(
    db_session: AsyncSession,
) -> None:
    """revoke_mcp_token sets revoked_at to the injected now on a live token."""
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    jti = uuid.uuid4()
    created_at = datetime.now(tz=UTC)

    await store.create_mcp_token_row(
        db_session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=str(uuid.uuid4()),
        label=None,
        created_at=created_at,
    )

    revoke_time = datetime.now(tz=UTC)
    revoked_row = await store.revoke_mcp_token(db_session, jti=jti, now=revoke_time)

    assert revoked_row is not None, "revoke_mcp_token must return the updated McpTokenRow"
    assert isinstance(revoked_row, McpTokenRow), (
        "revoke_mcp_token must return McpTokenRow (Pydantic), not the ORM"
    )
    assert revoked_row.revoked_at is not None, "revoked row must have revoked_at set after revoking"
    # Compare timestamps (strip microseconds for DB tz-aware comparison)
    assert abs((revoked_row.revoked_at - revoke_time).total_seconds()) < 1, (
        "revoked_at must match the injected now timestamp"
    )


async def test_revoke_mcp_token_is_noop_on_already_revoked_jti(
    db_session: AsyncSession,
) -> None:
    """revoke_mcp_token returns None when jti is already revoked (no-op, not an error)."""
    tenant_id, account_id = await _seed_tenant_and_account(db_session)
    jti = uuid.uuid4()
    now = datetime.now(tz=UTC)

    await store.create_mcp_token_row(
        db_session,
        jti=jti,
        account_id=account_id,
        tenant_id=tenant_id,
        agent_id=str(uuid.uuid4()),
        label=None,
        created_at=now,
    )

    # First revoke — should succeed
    first = await store.revoke_mcp_token(db_session, jti=jti, now=now)
    assert first is not None, "first revoke must return the updated row"

    # Second revoke — already revoked, must return None
    second = await store.revoke_mcp_token(db_session, jti=jti, now=now)
    assert second is None, "revoke_mcp_token on an already-revoked jti must return None (no-op)"


async def test_revoke_mcp_token_is_noop_on_unknown_jti(
    db_session: AsyncSession,
) -> None:
    """revoke_mcp_token returns None for a jti that does not exist (no-op)."""
    unknown_jti = uuid.uuid4()
    now = datetime.now(tz=UTC)

    result = await store.revoke_mcp_token(db_session, jti=unknown_jti, now=now)
    assert result is None, (
        "revoke_mcp_token on an unknown jti must return None (no-op, not an error)"
    )


@pytest.mark.parametrize("label", [None, "my-agent-token"])
async def test_get_mcp_token_returns_none_for_unknown_jti(
    db_session: AsyncSession,
    label: str | None,
) -> None:
    """get_mcp_token returns None when the jti does not exist."""
    unknown_jti = uuid.uuid4()
    row = await store.get_mcp_token(db_session, jti=unknown_jti)
    assert row is None, "get_mcp_token must return None for an unknown jti"
