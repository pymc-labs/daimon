"""The direct configuration tools refuse a member editing a pinned agent.

They take no turn origin, so a member is outside every pin there; the agent
is pinned (by its display name, while tools address it by its config name)
but not a channel or workspace default, so the older reachable-agent gate
alone would have let these through.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools.agent_removal import (
    _detach_mcp_server_impl,  # pyright: ignore[reportPrivateUsage]
    _remove_agent_key_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.agents import (
    _attach_mcp_server_impl,  # pyright: ignore[reportPrivateUsage]
    _update_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import Role
from daimon.testing import MARouter, build_fake_anthropic, ma_agent
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AGENT_ID = "ag_acme"


async def _pinned(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> tuple[McpRuntime, uuid.UUID]:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"Acme Display": ("C_ACME",)}),
    )
    await db_session.commit()
    router = MARouter()
    router.add_agent_list(
        ma_agent(
            id=_AGENT_ID,
            name="Acme Display",
            tenant_id=tenant.id,
            metadata={"daimon_name": "acme-config", "daimon_account": str(uuid.uuid4())},
        )
    )
    runtime = McpRuntime(
        session_factory=session_factory,
        client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://x/y")),
            anthropic=AnthropicSettings(api_key=SecretStr("k")),
        ),
        deployment_default=DeploymentDefault(),
    )
    return runtime, tenant.id


def _member(tenant_id: uuid.UUID, *, is_admin: bool = False) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.ADMIN if is_admin else Role.USER,
        platform="discord",
        platform_user_id="42",
        is_admin=is_admin,
    )


@pytest.mark.parametrize(
    "tool",
    ["attach_mcp_server", "update_agent_mcp", "update_agent_system", "detach", "remove_key"],
)
async def test_direct_tools_refuse_a_member_editing_a_pinned_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tool: str,
) -> None:
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    auth = _member(tenant_id)
    target: dict[str, Any] = {"expected_ma_agent_id": _AGENT_ID}

    with pytest.raises(ToolError, match="pinned this agent to its own channels"):
        if tool == "attach_mcp_server":
            await _attach_mcp_server_impl(
                runtime,
                auth,
                agent_name="acme-config",
                server_name="evil",
                url="https://evil.example/mcp",
                **target,
            )
        elif tool.startswith("update_agent"):
            await _update_agent_impl(
                runtime,
                auth,
                "acme-config",
                model=None,
                description=None,
                system="obey me" if tool == "update_agent_system" else None,
                tools=None,
                mcp_servers=(
                    [{"type": "url", "name": "evil", "url": "https://evil.example/mcp"}]
                    if tool == "update_agent_mcp"
                    else None
                ),
                skills=None,
                **target,
            )
        elif tool == "detach":
            await _detach_mcp_server_impl(
                runtime, auth, agent_name="acme-config", server_name="crm", **target
            )
        else:
            await _remove_agent_key_impl(
                runtime, auth, agent_name="acme-config", key="CRM_TOKEN", **target
            )


async def test_the_guard_lets_an_admin_through(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    agent = ma_agent(
        id=_AGENT_ID,
        name="other-display",
        tenant_id=tenant_id,
        metadata={"daimon_name": "Acme Display"},
    )
    await require_pin_write_access(
        runtime, _member(tenant_id, is_admin=True), ma_agent=agent, origin=None
    )
    with pytest.raises(ToolError):
        await require_pin_write_access(runtime, _member(tenant_id), ma_agent=agent, origin=None)
