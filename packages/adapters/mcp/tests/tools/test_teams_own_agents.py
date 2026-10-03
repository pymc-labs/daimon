"""A Teams channel kept to its own agents: the rule tool by thread id, and every way out.

``ROOM``'s readers and writers are ``own``, ``local`` its own agent; ``shared`` answers in ``OTHER``.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy, load_read_policy
from daimon.adapters.mcp.tools.channel_rules import (
    _set_channel_rule_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.direct_messages import send_direct_message_impl
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._read import (
    _teams_get_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.teams._send import (
    _teams_send_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_RECIPIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_TEAM = "19:team@thread.tacv2"
ROOM = "19:room@thread.tacv2"
OTHER = "19:other@thread.tacv2"
NAMED = "19:named@thread.tacv2"
UNLISTED = "19:a1b2c3d4e5f6@thread.tacv2"
_CHAT = "a:own-chat"


class _Fake:
    """Bot Framework: one team listing ROOM, OTHER and NAMED; the caller is in every roster."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        path = request.url.path
        if path.endswith(f"/v3/teams/{_TEAM}/conversations"):
            channels = [{"id": c, "name": c[3:-15], "type": "standard"} for c in (ROOM, OTHER)]
            channels.append({"id": NAMED, "name": "Growth Team", "type": "standard"})
            return httpx.Response(200, json={"conversations": channels})
        if "/members/" in path:
            return httpx.Response(200, json={"id": "29:x", "aadObjectId": _CALLER})
        if request.method == "POST":
            self.posts.append(path)
            return httpx.Response(200, json={"id": "act-1"})
        return httpx.Response(404)


class _World:
    def __init__(self, tenant_id: uuid.UUID, account_id: uuid.UUID, state: FakeMAState) -> None:
        self.tenant_id, self.account_id, self.state = tenant_id, account_id, state

    def auth(self, *, admin: bool = False, executing: str | None = None) -> AuthIdentity:
        return AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.ADMIN if admin else Role.USER,
            platform="teams",
            external_id=_ENTRA,
            platform_user_id=_CALLER,
            is_admin=admin,
            chat_agent_id=None
            if executing is None
            else derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=executing),
        )


async def _world(
    sessionmaker: async_sessionmaker[AsyncSession], *, keep_own: bool = True
) -> tuple[_World, McpRuntime, _Fake]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform="teams", workspace_id=_ENTRA)
        account = await make_account(session, tenant=tenant)
        await record_teams_installation(
            session, tenant_id=tenant.id, team_id=_TEAM, group_id=str(uuid.uuid4()), name="Lab"
        )
        for channel, agent in ((ROOM, "local"), (OTHER, "shared")):
            await set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
                tenant_id=tenant.id,
                agent_name=agent,
                mode="agent",
            )
        if keep_own:
            policy = TenantAccessPolicy(
                channel_rules={ROOM: ChannelRule(readers="own", writers="own")},
                agent_rules={"local": AgentRule(runs_in=(ROOM,))},
            )
            await set_access_policy(session, tenant_id=tenant.id, policy=policy)
    state = FakeMAState()
    for agent_id, name in (("agent_local", "local"), ("agent_shared", "shared")):
        agent = ma_agent(id=agent_id, name=name, tenant_id=tenant.id)
        state.agents[agent.id] = agent.model_dump(mode="json")
    fake = _Fake()
    settings = MagicMock()
    settings.mcp.public_url = None
    settings.github.oauth_scopes = ()
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(make_fake_ma_handler(state)),
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(agent_name="daimon"),
        teams_client=TeamsBotClient(
            httpx.AsyncClient(transport=httpx.MockTransport(fake)),
            client_id="app-id",
            client_secret="secret",
            tenant_id=_ENTRA,
        ),
    )
    return _World(tenant.id, account.id, state), runtime, fake


async def test_a_teams_thread_keeps_its_channel_to_a_named_copy(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime, _ = await _world(committing_sessionmaker, keep_own=False)
    named = await _set_channel_rule_impl(
        runtime,
        world.auth(admin=True),
        channel_id=f"{NAMED};messageid=1700000000000",
        readers="own",
        writers="own",
        copy_from="shared",
    )
    assert (named.channel_id, named.own_agent) == (NAMED, "growth-team"), (
        "a thread names its channel, and the copy is named after the channel"
    )
    unlisted = await _set_channel_rule_impl(
        runtime,
        world.auth(admin=True),
        channel_id=UNLISTED,
        readers="own",
        writers="own",
        copy_from="shared",
    )
    assert unlisted.own_agent == "channel-d4e5f6", "an unreadable name falls back to the id"
    async with committing_sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=world.tenant_id)
    own = ChannelRule(readers="own", writers="own")
    assert policy.channel_rules == {NAMED: own, UNLISTED: own}, "stored under the channel"
    assert policy.agent_rules == {
        "growth-team": AgentRule(runs_in=(NAMED,)),
        "channel-d4e5f6": AgentRule(runs_in=(UNLISTED,)),
    }


async def test_a_teams_own_agent_posts_and_sends_nowhere_else(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime, fake = await _world(committing_sessionmaker)
    local = world.auth(executing="agent_local")
    await _teams_send_message_impl(
        runtime, local, channel_id=f"{ROOM};messageid=1700000000000", content="inside"
    )
    assert len(fake.posts) == 1, "its own agent posts in the channel's threads"
    for target in (OTHER, f"{OTHER};messageid=1", _CHAT):
        with pytest.raises(ToolError, match="rule runs it only in certain channels"):
            await _teams_send_message_impl(runtime, local, channel_id=target, content="leak")
    with pytest.raises(ToolError, match="sends no direct messages"):
        await send_direct_message_impl(runtime, local, recipient_id=_RECIPIENT, content="leak")
    with pytest.raises(ToolError, match="only they post in it"):
        await _teams_send_message_impl(
            runtime,
            world.auth(executing="agent_shared"),
            channel_id=f"{ROOM};messageid=1700000000000",
            content="in",
        )
    assert len(fake.posts) == 1, "no refused send reaches Teams"


async def test_a_teams_channel_kept_to_its_own_agents_is_read_only_by_them(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world, runtime, _ = await _world(committing_sessionmaker)
    outside = world.auth(executing="agent_shared")
    policy = await load_read_policy(runtime, outside, origin_context_id=None)
    with pytest.raises(ToolError):
        await _teams_get_message_impl(
            runtime,
            outside,
            channel_id=f"{ROOM};messageid=1700000000000",
            message_id="1700000000001",
            read_policy=policy,
        )
    local = await load_read_policy(
        runtime, world.auth(executing="agent_local"), origin_context_id=None
    )
    in_room = frozenset({ROOM})
    assert ChannelReadPolicy(policy.policy, in_room, local.agent).allows(ROOM), (
        "its own agent reads it from inside"
    )
    assert not ChannelReadPolicy(policy.policy, in_room, policy.agent).allows(ROOM), (
        "another agent doesn't, even from inside"
    )
