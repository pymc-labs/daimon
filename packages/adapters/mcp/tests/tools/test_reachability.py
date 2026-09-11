"""Tests for require_admin_for_reachable_agent — the per-target runtime gate."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.reachability import require_admin_for_reachable_agent
from daimon.core.scope import DeploymentDefault, TenantScopeRef
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _runtime(session_factory: async_sessionmaker[AsyncSession] | MagicMock) -> McpRuntime:
    return McpRuntime(
        session_factory=session_factory,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


async def test_admin_short_circuits_without_touching_session_factory() -> None:
    """An admin caller returns immediately — the DB is never read."""
    session_factory = MagicMock()
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.ADMIN, is_admin=True
    )
    result = await require_admin_for_reachable_agent(
        _runtime(session_factory), auth, agent_name="daimon"
    )
    assert result is None, "admin caller passes the gate"
    session_factory.assert_not_called()


async def test_non_admin_reachable_agent_raises(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-admin caller patching a currently-reachable agent is refused."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord")
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant.id),
            tenant_id=tenant.id,
            agent_name="daimon",
            mode="agent",
        )

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant.id, role=Role.USER, is_admin=False
    )
    with pytest.raises(ToolError, match="admin must change"):
        await require_admin_for_reachable_agent(
            _runtime(db_session_factory), auth, agent_name="daimon"
        )


async def test_non_admin_unreachable_agent_returns_none(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-admin caller patching an agent nobody has scoped is not gated."""
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord")

    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant.id, role=Role.USER, is_admin=False
    )
    result = await require_admin_for_reachable_agent(
        _runtime(db_session_factory), auth, agent_name="my-agent"
    )
    assert result is None, "unreachable agent must not be gated for a non-admin caller"
