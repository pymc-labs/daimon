"""DB-backed unit tests for the propagation MCP tools.

Tests that admin set/clear persists at workspace and channel scope against real
Postgres (last-write-wins), and that non-admin callers are rejected with no write.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.propagation import (
    _clear_agent_default_impl,  # pyright: ignore[reportPrivateUsage]
    _set_agent_default_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import get_scope
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

_D28_MESSAGE = "Changing my setup needs Manage Server — ask a server admin to use /agent-setup"


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


def _admin_auth(*, tenant_id: uuid.UUID, account_id: uuid.UUID | None = None) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id or uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.ADMIN,
        is_admin=True,
    )


def _non_admin_auth(*, tenant_id: uuid.UUID) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        is_admin=False,
    )


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a Tenant + Account and return (tenant_id, account_id)."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        return tenant.id, account.id


# ---------------------------------------------------------------------------
# set_agent_default: workspace scope
# ---------------------------------------------------------------------------


async def test_set_agent_default_persists_at_workspace_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)

    result = await _set_agent_default_impl(_runtime(committing_sessionmaker), auth, "writer", None)

    assert result.agent_name == "writer", "result must echo back the agent_name that was set"
    assert result.scope == "workspace", "no channel_id means workspace scope"
    assert result.previous_agent_name is None, "scope had no prior default"

    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is not None, "set_agent_default must create a TenantConfig row"
    assert row.agent_name == "writer", "agent_name must be persisted at workspace scope"


async def test_set_agent_default_last_write_wins_at_workspace_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)

    await _set_agent_default_impl(_runtime(committing_sessionmaker), auth, "first-agent", None)
    result = await _set_agent_default_impl(
        _runtime(committing_sessionmaker), auth, "second-agent", None
    )

    assert result.previous_agent_name == "first-agent", (
        "second call must report the overwritten prior agent_name"
    )
    assert result.agent_name == "second-agent", "result must echo the newly-set agent_name"

    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is not None and row.agent_name == "second-agent", (
        "last-write-wins: workspace scope must hold the second agent_name"
    )


# ---------------------------------------------------------------------------
# set_agent_default: channel scope
# ---------------------------------------------------------------------------


async def test_set_agent_default_persists_at_channel_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)
    channel_id = "C123456"

    result = await _set_agent_default_impl(
        _runtime(committing_sessionmaker), auth, "channel-agent", channel_id
    )

    assert result.scope == f"channel:{channel_id}", "result must report the channel scope label"
    assert result.agent_name == "channel-agent", "result must echo back the agent_name"

    row = await get_scope(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
    )
    assert row is not None, "set_agent_default must create a ChannelConfig row"
    assert row.agent_name == "channel-agent", "agent_name must be persisted at channel scope"


# ---------------------------------------------------------------------------
# clear_agent_default
# ---------------------------------------------------------------------------


async def test_clear_agent_default_removes_workspace_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)

    await _set_agent_default_impl(_runtime(committing_sessionmaker), auth, "to-clear", None)
    result = await _clear_agent_default_impl(_runtime(committing_sessionmaker), auth, None)

    assert result.cleared is True, "cleared must be True when a default existed"

    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is None or row.agent_name is None, (
        "agent_name must be gone from workspace scope after clear"
    )


async def test_clear_agent_default_is_idempotent_when_no_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    auth = _admin_auth(tenant_id=tenant_id, account_id=account_id)

    result = await _clear_agent_default_impl(_runtime(committing_sessionmaker), auth, None)

    assert result.cleared is False, "cleared must be False when scope had no agent_name"


# ---------------------------------------------------------------------------
# non-admin rejection: no write performed
# ---------------------------------------------------------------------------


async def test_set_agent_default_raises_for_non_admin_and_performs_no_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id, _account_id = await _seed(committing_sessionmaker)
    auth = _non_admin_auth(tenant_id=tenant_id)

    with pytest.raises(ToolError) as exc_info:
        await _set_agent_default_impl(_runtime(committing_sessionmaker), auth, "any-agent", None)

    assert str(exc_info.value) == _D28_MESSAGE, (
        "non-admin caller must be refused with the D-28 message"
    )

    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is None, "no write must have been performed for a non-admin caller"


async def test_clear_agent_default_raises_for_non_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id = await _seed(committing_sessionmaker)
    auth = _non_admin_auth(tenant_id=tenant_id)

    with pytest.raises(ToolError) as exc_info:
        await _clear_agent_default_impl(_runtime(committing_sessionmaker), auth, None)

    assert str(exc_info.value) == _D28_MESSAGE, (
        "non-admin caller must be refused with the D-28 message for clear as well"
    )
