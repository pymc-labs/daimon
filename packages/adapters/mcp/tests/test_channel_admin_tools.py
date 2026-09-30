"""Channel admin tools, and what a channel admin may then do through MCP."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import propagation
from daimon.adapters.mcp.tools.channel_admins import (
    _clear_channel_admins_impl,  # pyright: ignore[reportPrivateUsage]
    _list_channel_admins_impl,  # pyright: ignore[reportPrivateUsage]
    _set_channel_admins_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.reachability import require_admin_for_reachable_agent
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_account, make_routine, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CHANNEL = "111111111111111111"
OTHER_CHANNEL = "222222222222222222"
ROLE = "333333333333333333"
USER = "444444444444444444"


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
    )


def _auth(tenant_id: uuid.UUID, *, admin: bool = False, platform: str = "discord") -> AuthIdentity:
    return AuthIdentity(
        account_id=_ACCOUNTS[tenant_id],
        tenant_id=tenant_id,
        role=Role.ADMIN if admin else Role.USER,
        platform=platform,
        platform_user_id=USER,
        platform_role_ids=(),
        is_admin=admin,
    )


_ACCOUNTS: dict[uuid.UUID, uuid.UUID] = {}


async def _tenant(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        _ACCOUNTS[tenant.id] = (await make_account(session, tenant=tenant)).id
        return tenant.id


async def test_server_admin_sets_lists_and_clears_channel_admins(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(committing_sessionmaker)
    runtime, admin = _runtime(committing_sessionmaker), _auth(tenant_id, admin=True)

    assert (await _list_channel_admins_impl(runtime, admin)).channels == []
    result = await _set_channel_admins_impl(
        runtime, admin, channel_id=CHANNEL, role_ids=[ROLE, ROLE], user_ids=[]
    )
    assert result.channel.role_ids == [ROLE] and result.changed
    listed = await _list_channel_admins_impl(runtime, admin)
    assert [c.channel_id for c in listed.channels] == [CHANNEL]

    cleared = await _set_channel_admins_impl(
        runtime, admin, channel_id=CHANNEL, role_ids=[], user_ids=[]
    )
    assert cleared.changed, "two empty lists clear the channel"
    again = await _clear_channel_admins_impl(runtime, admin, channel_id=CHANNEL)
    assert not again.changed, "clearing twice is a no-op"


async def test_channel_admin_tools_refuse_members_and_bad_ids(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    with pytest.raises(ToolError, match="requires a workspace or server admin"):
        await _set_channel_admins_impl(
            runtime, _auth(tenant_id), channel_id=CHANNEL, role_ids=[], user_ids=[USER]
        )
    with pytest.raises(ToolError, match="no roles"):
        await _set_channel_admins_impl(
            runtime,
            _auth(tenant_id, admin=True, platform="slack"),
            channel_id="C0123",
            role_ids=["S1"],
            user_ids=[],
        )
    with pytest.raises(ToolError, match="invalid discord channel id"):
        await _set_channel_admins_impl(
            runtime, _auth(tenant_id, admin=True), channel_id="#general", role_ids=[], user_ids=[]
        )


async def test_channel_admin_sets_only_their_own_channels_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(propagation, "resolve_setup_agent", AsyncMock())
    tenant_id = await _tenant(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    member = _auth(tenant_id)

    with pytest.raises(ToolError, match="admin of that channel"):
        await propagation._set_agent_default_impl(runtime, member, "helper", CHANNEL)  # pyright: ignore[reportPrivateUsage]
    await _set_channel_admins_impl(
        runtime, _auth(tenant_id, admin=True), channel_id=CHANNEL, role_ids=[], user_ids=[USER]
    )

    await propagation._set_agent_default_impl(runtime, member, "helper", CHANNEL)  # pyright: ignore[reportPrivateUsage]
    row = await get_scope(
        db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL)
    )
    assert row is not None and row.agent_name == "helper"
    with pytest.raises(ToolError, match="admin of that channel"):
        await propagation._set_agent_default_impl(runtime, member, "helper", OTHER_CHANNEL)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ToolError, match="requires a workspace or server admin"):
        await propagation._clear_agent_default_impl(runtime, member, None)  # pyright: ignore[reportPrivateUsage]
    cleared = await propagation._clear_agent_default_impl(runtime, member, CHANNEL)  # pyright: ignore[reportPrivateUsage]
    assert cleared.cleared
    assert await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id)) is None


async def test_channel_admin_may_edit_an_agent_local_to_their_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        for channel in (CHANNEL, OTHER_CHANNEL):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel),
                tenant_id=tenant_id,
                agent_name="helper" if channel == CHANNEL else "shared",
                mode="agent",
            )
    role_member = AuthIdentity(
        account_id=_ACCOUNTS[tenant_id],
        tenant_id=tenant_id,
        role=Role.USER,
        platform="discord",
        platform_user_id="555555555555555555",
        platform_role_ids=(ROLE,),
    )
    with pytest.raises(ToolError, match="an admin must change its setup"):
        await require_admin_for_reachable_agent(runtime, role_member, agent_name="helper")

    await _set_channel_admins_impl(
        runtime, _auth(tenant_id, admin=True), channel_id=CHANNEL, role_ids=[ROLE], user_ids=[]
    )
    await require_admin_for_reachable_agent(runtime, role_member, agent_name="helper")
    with pytest.raises(ToolError, match="an admin must change its setup"):
        await require_admin_for_reachable_agent(runtime, role_member, agent_name="shared")


async def test_channel_admin_cannot_bind_another_channels_own_agent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve = AsyncMock(return_value=SimpleNamespace(metadata={}))
    monkeypatch.setattr(propagation, "resolve_setup_agent", resolve)
    tenant_id = await _tenant(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=OTHER_CHANNEL),
            tenant_id=tenant_id,
            agent_name="other-own",
            mode="agent",
        )
    await _set_channel_admins_impl(
        runtime, _auth(tenant_id, admin=True), channel_id=CHANNEL, role_ids=[], user_ids=[USER]
    )
    member = _auth(tenant_id)

    with pytest.raises(ToolError, match="does not administer"):
        await propagation._set_agent_default_impl(runtime, member, "other-own", CHANNEL)  # pyright: ignore[reportPrivateUsage]
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL))
        is None
    ), "the refused bind wrote nothing"

    resolve.return_value = SimpleNamespace(metadata={MA_METADATA_KEY_MANAGED: "true"})
    await propagation._set_agent_default_impl(runtime, member, "other-own", CHANNEL)  # pyright: ignore[reportPrivateUsage]
    resolve.return_value = SimpleNamespace(metadata={})
    await propagation._set_agent_default_impl(  # pyright: ignore[reportPrivateUsage]
        runtime, _auth(tenant_id, admin=True), "other-own", CHANNEL
    )


async def test_channel_admin_loses_an_agent_that_runs_someone_elses_routine(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL),
            tenant_id=tenant_id,
            agent_name="helper",
            mode="agent",
        )
    await _set_channel_admins_impl(
        runtime, _auth(tenant_id, admin=True), channel_id=CHANNEL, role_ids=[], user_ids=[USER]
    )
    member = _auth(tenant_id)
    await require_admin_for_reachable_agent(runtime, member, agent_name="helper")

    async with committing_sessionmaker.begin() as session:
        tenant = await get_tenant(session, tenant_id)
        assert tenant is not None, "the tenant exists"
        await make_routine(
            session, tenant=tenant, created_by_user_id="666666666666666666", agent_name="helper"
        )
    with pytest.raises(ToolError, match="an admin must change its setup"):
        await require_admin_for_reachable_agent(runtime, member, agent_name="helper")
