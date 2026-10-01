"""The direct configuration tools refuse a member editing a pinned agent.

They take no turn origin, so a member is outside every pin there; the agent
is pinned (by its display name, while tools address it by its config name)
but not a channel or workspace default, so the older reachable-agent gate
alone would have let these through.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools.agent_removal import (
    _detach_mcp_server_impl,  # pyright: ignore[reportPrivateUsage]
    _remove_agent_key_impl,  # pyright: ignore[reportPrivateUsage]
    _remove_skill_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.agents import (
    _attach_mcp_server_impl,  # pyright: ignore[reportPrivateUsage]
    _update_agent_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.repo_binding import (
    _bind_public_repo_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.self_edit import (
    _clear_repo_binding_impl,  # pyright: ignore[reportPrivateUsage]
    _self_delete_file_impl,  # pyright: ignore[reportPrivateUsage]
    _self_write_file_impl,  # pyright: ignore[reportPrivateUsage]
    _set_repo_binding_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.turn_origins import create_origin
from daimon.testing import MARouter, build_fake_anthropic, ma_agent
from daimon.testing.factories import make_account, make_tenant
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
    [
        "attach_mcp_server",
        "update_agent_mcp",
        "update_agent_system",
        "detach",
        "remove_key",
        "remove_skill",
    ],
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
        elif tool == "remove_key":
            await _remove_agent_key_impl(
                runtime, auth, agent_name="acme-config", key="CRM_TOKEN", **target
            )
        else:
            await _remove_skill_impl(
                runtime, auth, agent_name="acme-config", skill_id="skill_1", **target
            )


@pytest.mark.parametrize(
    "tool", ["set_repo_binding", "clear_repo_binding", "self_write_file", "self_delete_file"]
)
async def test_an_agent_key_cannot_rebind_a_pinned_agents_repo(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tool: str,
) -> None:
    """An agent key carries no turn origin, so a member's key is outside every pin."""
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_AGENT_ID),
    )

    with pytest.raises(ToolError, match="pinned this agent to its own channels"):
        if tool == "set_repo_binding":
            await _set_repo_binding_impl(
                runtime, auth, repo_url="https://github.com/evil/repo", default_branch="main"
            )
        elif tool == "clear_repo_binding":
            await _clear_repo_binding_impl(runtime, auth)
        elif tool == "self_write_file":
            await _self_write_file_impl(runtime, auth, key="CRM_TOKEN", content="evil")
        else:
            await _self_delete_file_impl(runtime, auth, key="CRM_TOKEN")


async def test_bind_public_repo_refuses_a_member_outside_a_pinned_agents_channels(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    tenant_account = await make_account(db_session, tenant=await get_tenant(db_session, tenant_id))
    await db_session.commit()
    auth = AuthIdentity(
        account_id=tenant_account.id,
        tenant_id=tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
    )
    async with db_session_factory.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant_id,
            account_id=auth.account_id,
            platform="discord",
            parent_channel_id="C_CLIENTB",
            thread_id="T1",
            responder_ma_agent_id="ag_clientb",
            responder_name="clientb-project",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            now=datetime.now(UTC),
        )

    with pytest.raises(ToolError, match="pinned this agent to its own channels"):
        await _bind_public_repo_impl(
            runtime,
            auth,
            agent_name="acme-config",
            repo_url="https://github.com/evil/repo",
            branch="main",
            origin_context_id=str(origin.id),
            expected_ma_agent_id=_AGENT_ID,
            unsaved_work=None,
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


async def test_the_guard_lets_an_admin_of_every_pinned_channel_through_without_an_origin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A direct tool has no origin; the admin of the pin's channel may still use it."""
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    agent = ma_agent(
        id=_AGENT_ID,
        name="Acme Display",
        tenant_id=tenant_id,
        metadata={"daimon_name": "acme-config"},
    )
    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id="C_OTHER",
        role_ids=[],
        user_ids=["42"],
        actor_account_id=None,
    )
    await db_session.commit()
    with pytest.raises(ToolError, match="pinned this agent"):
        await require_pin_write_access(runtime, _member(tenant_id), ma_agent=agent, origin=None)

    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id="C_ACME",
        role_ids=[],
        user_ids=["42"],
        actor_account_id=None,
    )
    await db_session.commit()
    await require_pin_write_access(runtime, _member(tenant_id), ma_agent=agent, origin=None)
    agent_key = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id="42",
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=_AGENT_ID),
    )
    with pytest.raises(ToolError, match="pinned this agent"):
        await require_pin_write_access(runtime, agent_key, ma_agent=agent, origin=None)


async def test_the_guard_refuses_a_channel_admin_of_only_part_of_a_pin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    runtime, tenant_id = await _pinned(db_session, db_session_factory)
    await set_access_policy(
        db_session,
        tenant_id=tenant_id,
        policy=TenantAccessPolicy(agent_channel_pins={"Acme Display": ("C_ACME", "C_OPS")}),
    )
    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id="C_ACME",
        role_ids=[],
        user_ids=["42"],
        actor_account_id=None,
    )
    await db_session.commit()
    agent = ma_agent(id=_AGENT_ID, name="Acme Display", tenant_id=tenant_id, metadata={})
    with pytest.raises(ToolError, match="pinned this agent"):
        await require_pin_write_access(runtime, _member(tenant_id), ma_agent=agent, origin=None)
