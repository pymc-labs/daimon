"""Channel environment tools: who may pick a scope's environment, and what a pick stores."""

from __future__ import annotations

import dataclasses
import re
import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic.types.beta import BetaCloudConfig, BetaLimitedNetwork
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _channel_target as channel_target
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
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_environments import save_scope_environment
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import list_administered_channel_ids
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_read import get_scope
from daimon.testing import EMPTY_CLOUD_CONFIG, ma_environment
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CHANNEL = "111111111111111111"
OTHER_CHANNEL = "222222222222222222"
USER = "444444444444444444"
THREAD = "333333333333333333"
"""A thread under CHANNEL."""
ROLE = "666666666666666666"
OTHER_ROLE = "777777777777777777"
HIDDEN = "555555555555555555"
"""A channel the Discord lookup refuses: deleted, or not visible to the caller."""


@pytest.fixture(autouse=True)
def _visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the platform lookups: THREAD's parent is CHANNEL, the rest is visible."""

    async def fake(_runtime: McpRuntime, _auth: AuthIdentity, channel_id: str) -> str:
        if channel_id == HIDDEN:
            raise ToolError("that channel is not visible to the caller")
        return CHANNEL if channel_id == THREAD else channel_id

    monkeypatch.setattr(channel_target, "resolve_visible_channel", fake)
    monkeypatch.setattr(channel_target, "_visible_slack_channel", fake)
    monkeypatch.setattr(channel_target, "_visible_teams_channel", fake)


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        return tenant.id, (await make_account(session, tenant=tenant)).id


LIMITED = BetaCloudConfig(
    type="cloud",
    networking=BetaLimitedNetwork(
        type="limited", allowed_hosts=[], allow_mcp_servers=False, allow_package_managers=False
    ),
    packages=EMPTY_CLOUD_CONFIG.packages,
)
"""A cloud environment that reaches no host but its own."""


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *names: str,
    limited: tuple[str, ...] = (),
) -> McpRuntime:
    """Environments `names` (unrestricted networking) and `limited`, in that tenant."""
    router = MARouter()
    router.add_environment_list(
        *(ma_environment(id=f"env_{name}", name=name, tenant_id=tenant_id) for name in names),
        *(
            ma_environment(id=f"env_{name}", name=name, tenant_id=tenant_id, config=LIMITED)
            for name in limited
        ),
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(router.dispatch),
        settings=MagicMock(),  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )


def _auth(
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    admin: bool = False,
    platform: str = "discord",
    role_ids: tuple[str, ...] = (),
    agent_id: uuid.UUID | None = None,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant_id,
        role=Role.ADMIN if admin else Role.USER,
        platform=platform,
        platform_user_id=USER,
        is_admin=admin,
        platform_role_ids=role_ids,
        agent_id=agent_id,
    )


async def _verified(runtime: McpRuntime, auth: AuthIdentity) -> AuthIdentity:
    """`auth` with the channel admin grants the token verifier reads on each request."""
    if auth.agent_id is not None or auth.is_admin:
        return auth
    async with runtime.session_factory() as session:
        administered = await list_administered_channel_ids(
            session,
            tenant_id=auth.tenant_id,
            platform=auth.platform or "",
            platform_user_id=USER,
            role_ids=auth.platform_role_ids,
        )
    return dataclasses.replace(auth, administered_channel_ids=frozenset(administered))


async def _grant(
    runtime: McpRuntime,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    role_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
) -> None:
    await _set_channel_admins_impl(
        runtime,
        _auth(tenant_id, account_id, admin=True),
        channel_id=CHANNEL,
        role_ids=role_ids or [],
        user_ids=user_ids or [],
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
    assert tenant_row is not None and tenant_row.environment_name == "shared", (
        "the workspace row is committed"
    )


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
    member = await _verified(runtime, member)

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
    assert cleared.changed and cleared.previous_environment_name == "science", (
        "a channel admin clears their channel's pick"
    )
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
    assert "deployment default" in before.environment_explanation, "the note names the tier"
    assert (after.channel_environment, after.tenant_environment, after.deployment_environment) == (
        "science",
        "shared",
        "default",
    ), "every tier's own environment is reported"
    assert after.environment_winning_tier == "channel", "the channel's own pick wins"
    assert "science" in after.environment_explanation, "the note names the environment"
    assert after.effective_agent_name == "daimon", "the agent is untouched by an environment"


async def test_a_channel_admin_in_a_thread_picks_its_parent_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")
    await _grant(runtime, tenant_id, account_id, user_ids=[USER])
    member = await _verified(runtime, _auth(tenant_id, account_id))

    result = await _set_channel_environment_impl(
        runtime, member, environment_name="science", channel_id=THREAD
    )
    cleared = await _clear_channel_environment_impl(runtime, member, channel_id=THREAD)

    assert result.scope == f"channel:{CHANNEL}", "a thread id is stored under its parent"
    assert cleared.changed, "clearing from the thread clears the parent's pick"
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=THREAD))
        is None
    ), "nothing is ever stored under the thread id"


async def test_a_slack_thread_id_resolves_to_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")
    admin = _auth(tenant_id, account_id, admin=True, platform="slack")

    result = await _set_channel_environment_impl(
        runtime, admin, environment_name="science", channel_id="C0GROWTH:1717.5"
    )

    assert result.scope == "channel:C0GROWTH", "a Slack thread id is stored under its channel"
    row = await get_scope(
        db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="C0GROWTH")
    )
    assert row is not None and row.environment_name == "science", "the channel row is written"


async def test_a_teams_thread_id_resolves_to_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """A Teams thread id is never stored as a channel, where no turn would read it."""
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")
    admin = _auth(tenant_id, account_id, admin=True, platform="teams")
    channel = "19:growth@thread.tacv2"

    result = await _set_channel_environment_impl(
        runtime, admin, environment_name="science", channel_id=f"{channel};messageid=1717"
    )

    assert result.scope == f"channel:{channel}", "a Teams thread id is stored under its channel"
    row = await get_scope(
        db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel)
    )
    assert row is not None and row.environment_name == "science", "the channel row is written"


@pytest.mark.parametrize(
    ("platform", "channel_id"),
    [("discord", ""), ("discord", "  "), ("slack", ":1717.5"), ("slack", " :")],
)
async def test_an_empty_channel_id_is_refused_rather_than_read_as_the_workspace(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    platform: str,
    channel_id: str,
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")
    admin = _auth(tenant_id, account_id, admin=True, platform=platform)

    with pytest.raises(ToolError, match="channel_id is empty"):
        await _set_channel_environment_impl(
            runtime, admin, environment_name="science", channel_id=channel_id
        )
    with pytest.raises(ToolError, match="channel_id is empty"):
        await _clear_channel_environment_impl(runtime, admin, channel_id=channel_id)
    assert await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant_id)) is None, (
        "an empty id never writes the workspace default"
    )


async def test_a_role_grant_admits_holders_of_that_role_only(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")
    await _grant(runtime, tenant_id, account_id, role_ids=[ROLE])

    result = await _set_channel_environment_impl(
        runtime,
        await _verified(runtime, _auth(tenant_id, account_id, role_ids=(ROLE,))),
        environment_name="science",
        channel_id=CHANNEL,
    )
    assert result.changed, "a member holding the granted role picks the channel's environment"
    with pytest.raises(ToolError, match="admin of that channel"):
        await _set_channel_environment_impl(
            runtime,
            await _verified(runtime, _auth(tenant_id, account_id, role_ids=(OTHER_ROLE,))),
            environment_name="science",
            channel_id=CHANNEL,
        )


async def test_an_agent_credential_is_never_a_channel_admin(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")
    await _grant(runtime, tenant_id, account_id, user_ids=[USER])
    agent = dataclasses.replace(
        _auth(tenant_id, account_id, agent_id=uuid.uuid4()),
        administered_channel_ids=frozenset({CHANNEL}),
    )

    with pytest.raises(ToolError, match="admin of that channel"):
        await _set_channel_environment_impl(
            runtime, agent, environment_name="science", channel_id=CHANNEL
        )
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL))
        is None
    ), "an agent acting for a granted user still writes nothing"


async def test_a_pick_on_a_channel_the_lookup_refuses_is_refused_but_can_be_cleared(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")
    admin = _auth(tenant_id, account_id, admin=True)
    async with committing_sessionmaker.begin() as session:
        await save_scope_environment(
            session,
            tenant_id=tenant_id,
            channel_id=HIDDEN,
            environment_name="science",
            actor_account_id=account_id,
        )

    with pytest.raises(ToolError, match="not visible"):
        await _set_channel_environment_impl(
            runtime, admin, environment_name="science", channel_id=HIDDEN
        )
    cleared = await _clear_channel_environment_impl(runtime, admin, channel_id=HIDDEN)

    assert cleared.changed, "a deleted or hidden channel's pick can still be cleared"
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=HIDDEN))
        is None
    ), "the cleared row is gone"


async def _seal(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, channel_id: str
) -> None:
    async with sessionmaker.begin() as session:
        await set_access_policy(
            session,
            tenant_id=tenant_id,
            policy=TenantAccessPolicy(sealed_channel_ids=(channel_id,)),
        )


async def test_a_channel_admin_never_opens_the_network_of_a_sealed_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """In a sealed channel an unrestricted network is a server admin's call; a limited one isn't."""
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "open", limited=("closed",))
    await _grant(runtime, tenant_id, account_id, user_ids=[USER])
    await _seal(committing_sessionmaker, tenant_id, CHANNEL)
    member = await _verified(runtime, _auth(tenant_id, account_id))

    with pytest.raises(ToolError, match="sealed.*unrestricted network"):
        await _set_channel_environment_impl(
            runtime, member, environment_name="open", channel_id=CHANNEL
        )
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL))
        is None
    ), "the refused pick writes nothing"
    closed = await _set_channel_environment_impl(
        runtime, member, environment_name="closed", channel_id=CHANNEL
    )
    assert closed.changed, "a limited network stays the channel admin's pick"
    opened = await _set_channel_environment_impl(
        runtime,
        _auth(tenant_id, account_id, admin=True),
        environment_name="open",
        channel_id=CHANNEL,
    )
    assert opened.changed, "a server admin may pick an unrestricted network there"
    with pytest.raises(ToolError, match="sealed"):
        await _set_channel_environment_impl(
            runtime, member, environment_name="open", channel_id=THREAD
        )


async def test_a_channel_admin_clears_a_sealed_pick_only_onto_a_limited_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Clearing falls back to the workspace default, so its network decides the clear."""
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "open", limited=("closed",))
    admin = _auth(tenant_id, account_id, admin=True)
    await _grant(runtime, tenant_id, account_id, user_ids=[USER])
    await _seal(committing_sessionmaker, tenant_id, CHANNEL)
    member = await _verified(runtime, _auth(tenant_id, account_id))
    await _set_channel_environment_impl(runtime, admin, environment_name="open", channel_id=None)
    await _set_channel_environment_impl(
        runtime, admin, environment_name="closed", channel_id=CHANNEL
    )

    with pytest.raises(ToolError, match="sealed.*the default it would fall back to"):
        await _clear_channel_environment_impl(runtime, member, channel_id=CHANNEL)
    await _set_channel_environment_impl(runtime, admin, environment_name="closed", channel_id=None)
    cleared = await _clear_channel_environment_impl(runtime, member, channel_id=CHANNEL)
    assert cleared.changed, "onto a limited workspace default the channel admin may clear"


async def test_a_channel_admin_pick_looks_its_environment_up_once(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    """A name missing when the network rule runs is refused, even if it appears before the write.

    Otherwise an open environment created under that name in between would
    land in a sealed channel without the rule ever judging it.
    """
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id)
    appeared = [
        ma_environment(id="env_late", name="late", tenant_id=tenant_id).model_dump(mode="json")
    ]
    listings: list[int] = []

    def environments(_request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        listings.append(1)
        return list_response([] if len(listings) == 1 else appeared)

    router = MARouter()
    router.add("GET", r"/v1/environments", environments)
    runtime = dataclasses.replace(runtime, client=build_fake_anthropic(router.dispatch))
    await _grant(runtime, tenant_id, account_id, user_ids=[USER])
    await _seal(committing_sessionmaker, tenant_id, CHANNEL)
    member = await _verified(runtime, _auth(tenant_id, account_id))

    with pytest.raises(ToolError, match="No environment named 'late'"):
        await _set_channel_environment_impl(
            runtime, member, environment_name="late", channel_id=CHANNEL
        )
    assert len(listings) == 1, "the pick looks the environment up once"
    assert (
        await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=CHANNEL))
        is None
    ), "a pick of a missing environment writes nothing"


async def test_a_sealed_discord_thread_keeps_its_channel_network_closed(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The environment covers every thread under a channel, so a seal on one thread counts."""
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "open")
    await _grant(runtime, tenant_id, account_id, user_ids=[USER])
    await _seal(committing_sessionmaker, tenant_id, THREAD)
    member = await _verified(runtime, _auth(tenant_id, account_id))

    with pytest.raises(ToolError, match="sealed.*unrestricted network"):
        await _set_channel_environment_impl(
            runtime, member, environment_name="open", channel_id=THREAD
        )


async def test_an_operator_token_needs_channels_write_and_a_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`channels:write` opens a channel's environment to an operator, never the workspace's."""
    tenant_id, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker, tenant_id, "science")

    def operator(*scopes: str) -> AuthIdentity:
        return dataclasses.replace(
            _auth(tenant_id, account_id, admin=True),
            token_kind="operator",
            token_jti=uuid.uuid4(),
            scopes=frozenset(scopes),
        )

    with pytest.raises(ToolError, match="does not have the channels:write scope"):
        await _set_channel_environment_impl(
            runtime, operator("tenant:read"), environment_name="science", channel_id=CHANNEL
        )
    with pytest.raises(ToolError, match="only a channel's environment"):
        await _set_channel_environment_impl(
            runtime, operator("channels:write"), environment_name="science", channel_id=None
        )
    result = await _set_channel_environment_impl(
        runtime, operator("channels:write"), environment_name="science", channel_id=CHANNEL
    )
    cleared = await _clear_channel_environment_impl(
        runtime, operator("channels:write"), channel_id=CHANNEL
    )
    assert result.changed and cleared.changed, "with the scope it sets and clears a channel's"
