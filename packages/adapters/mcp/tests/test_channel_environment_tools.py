"""Channel environment tools: who may pick a scope's environment, and what a pick stores."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.channel_admins import (
    _set_channel_admins_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.channel_environments import (
    _clear_channel_environment_impl,  # pyright: ignore[reportPrivateUsage]
    _set_channel_environment_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.propagation import (
    _explain_agent_resolution_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import get_scope
from daimon.testing import ma_environment
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CHANNEL = "111111111111111111"
OTHER_CHANNEL = "222222222222222222"
USER = "444444444444444444"


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        return tenant.id, (await make_account(session, tenant=tenant)).id


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, *names: str
) -> McpRuntime:
    router = MARouter()
    router.add_environment_list(
        *(ma_environment(id=f"env_{name}", name=name, tenant_id=tenant_id) for name in names)
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(router.dispatch),
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )


def _auth(tenant_id: uuid.UUID, account_id: uuid.UUID, *, admin: bool = False) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.ADMIN if admin else Role.USER,
        platform="discord",
        platform_user_id=USER,
        is_admin=admin,
    )


async def test_server_admin_sets_a_channel_and_the_workspace_environment(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science", "shared")
    admin = _auth(tenant_id, account_id, admin=True)

    channel = await _set_channel_environment_impl(
        runtime, admin, environment_name=" science ", channel_id=CHANNEL
    )
    workspace = await _set_channel_environment_impl(
        runtime, admin, environment_name="shared", channel_id=None
    )
    again = await _set_channel_environment_impl(
        runtime, admin, environment_name="science", channel_id=CHANNEL
    )

    assert (channel.scope, channel.environment_name, channel.changed) == (
        f"channel:{CHANNEL}",
        "science",
        True,
    ), "the channel scope names the trimmed environment"
    assert workspace.scope == "workspace", "no channel id writes the workspace default"
    assert not again.changed and again.previous_environment_name == "science", (
        "setting the same environment twice reports no change"
    )
    row = await get_scope(
        db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL)
    )
    tenant_row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id))
    assert row is not None and row.environment_name == "science", "the channel row is committed"
    assert row.agent_name is None, "picking an environment leaves the channel's agent alone"
    assert tenant_row is not None and tenant_row.environment_name == "shared"


async def test_unknown_environment_is_refused_without_a_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")

    with pytest.raises(ToolError, match="No environment named 'gpu'"):
        await _set_channel_environment_impl(
            runtime,
            _auth(tenant_id, account_id, admin=True),
            environment_name="gpu",
            channel_id=CHANNEL,
        )
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL))
        is None
    ), "a refused pick writes nothing"


async def test_another_tenants_environment_is_not_found(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, uuid.uuid4(), "science")

    with pytest.raises(ToolError, match="No environment named 'science'"):
        await _set_channel_environment_impl(
            runtime,
            _auth(tenant_id, account_id, admin=True),
            environment_name="science",
            channel_id=CHANNEL,
        )


async def test_members_are_refused_and_channel_admins_act_on_their_channel_only(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")
    member = _auth(tenant_id, account_id)

    with pytest.raises(ToolError, match="admin of that channel"):
        await _set_channel_environment_impl(
            runtime, member, environment_name="science", channel_id=CHANNEL
        )
    await _set_channel_admins_impl(
        runtime,
        _auth(tenant_id, account_id, admin=True),
        channel_id=CHANNEL,
        role_ids=[],
        user_ids=[USER],
    )

    result = await _set_channel_environment_impl(
        runtime, member, environment_name="science", channel_id=CHANNEL
    )
    assert result.changed, "a channel admin picks their own channel's environment"
    with pytest.raises(ToolError, match="admin of that channel"):
        await _set_channel_environment_impl(
            runtime, member, environment_name="science", channel_id=OTHER_CHANNEL
        )
    with pytest.raises(ToolError, match="requires a workspace or server admin"):
        await _set_channel_environment_impl(
            runtime, member, environment_name="science", channel_id=None
        )
    with pytest.raises(ToolError, match="requires a workspace or server admin"):
        await _clear_channel_environment_impl(runtime, member, channel_id=None)

    cleared = await _clear_channel_environment_impl(runtime, member, channel_id=CHANNEL)
    again = await _clear_channel_environment_impl(runtime, member, channel_id=CHANNEL)
    assert cleared.changed and cleared.previous_environment_name == "science"
    assert not again.changed and "nothing changed" in again.note, "a second clear is a no-op"
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL))
        is None
    ), "the cleared channel is back to no row, exactly as before any pick"


async def test_explain_reports_each_tiers_environment(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science", "shared")
    admin = _auth(tenant_id, account_id, admin=True)

    before = await _explain_agent_resolution_impl(runtime, admin, CHANNEL)
    await _set_channel_environment_impl(runtime, admin, environment_name="shared", channel_id=None)
    await _set_channel_environment_impl(
        runtime, admin, environment_name="science", channel_id=CHANNEL
    )
    after = await _explain_agent_resolution_impl(runtime, admin, CHANNEL)

    assert (before.effective_environment_name, before.environment_winning_tier) == (
        "default",
        "deployment",
    ), "with nothing set the deployment default decides"
    assert "deployment default" in before.environment_explanation
    assert (after.channel_environment, after.tenant_environment, after.deployment_environment) == (
        "science",
        "shared",
        "default",
    ), "every tier's own environment is reported"
    assert after.environment_winning_tier == "channel", "the channel's own pick wins"
    assert "science" in after.environment_explanation
    assert after.effective_agent_name == "daimon", "the agent is untouched by an environment"
