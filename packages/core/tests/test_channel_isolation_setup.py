"""Turning channel isolation on and off, and the fork that gives a channel its own agent."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import SkillListResponse
from daimon.core import channel_isolation_setup
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_fork import AgentCopy, fork_agent
from daimon.core.agent_mcp_credentials import save_agent_mcp_credential
from daimon.core.authz import Subject
from daimon.core.channel_environments import SEALED_OPEN_NETWORK_WARNING
from daimon.core.channel_isolation_setup import (
    END_ISOLATION_WARNING,
    LIFT_ISOLATION_WARNING,
    ChannelIsolationRefused,
    IsolationChange,
    isolated_agent_name,
    render_isolation_refusal,
    set_channel_isolation,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    tenant_scoped_display_title,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ADMIN = Subject(is_admin=True)
DEFAULT = DeploymentDefault(agent_name="daimon")


def test_isolated_agent_name_slugs_the_channel_and_avoids_taken_names() -> None:
    assert isolated_agent_name("Team Alpha!", "123", taken=()) == "team-alpha"
    assert isolated_agent_name("Team Alpha", "123", taken={"team-alpha"}) == "team-alpha-2"
    assert isolated_agent_name(None, "C0ABCDEF12", taken=()) == "channel-cdef12", (
        "no label falls back to the channel id's tail"
    )


def _client(tenant_id: uuid.UUID, *agents: tuple[str, bool]) -> tuple[AsyncAnthropic, FakeMAState]:
    state = FakeMAState()
    for index, (name, managed) in enumerate(agents):
        metadata = {MA_METADATA_KEY_MANAGED: "true"} if managed else {}
        agent = ma_agent(id=f"agent_{index}", name=name, tenant_id=tenant_id, metadata=metadata)
        state.agents[agent.id] = agent.model_dump(mode="json")
    return build_fake_anthropic(make_fake_ma_handler(state)), state


async def _bind(session: AsyncSession, tenant_id: uuid.UUID, channel_id: str, agent: str) -> None:
    await set_fields(
        session,
        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
        tenant_id=tenant_id,
        agent_name=agent,
        mode="agent",
    )


async def _isolate(
    client: AsyncAnthropic,
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    channel_id: str,
    *,
    isolated: bool = True,
    **kwargs: Any,
) -> IsolationChange:
    return await set_channel_isolation(
        client,
        factory,
        tenant_id=tenant_id,
        platform="discord",
        channel_id=channel_id,
        isolated=isolated,
        default=DEFAULT,
        actor_account_id=None,
        subject=ADMIN,
        **kwargs,
    )


async def test_isolating_warns_of_the_channels_own_open_environment(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A pick made before the seal skipped its network rule, so isolating says so; never refuses."""
    tenant = await make_tenant(db_session)
    state = FakeMAState()
    agent = ma_agent(id="agent_local", name="local", tenant_id=tenant.id)
    state.agents[agent.id] = agent.model_dump(mode="json")
    environments = [ma_environment(id="env_open", name="open", tenant_id=tenant.id)]

    def list_environments(request: httpx.Request) -> httpx.Response:
        if request.method != "GET" or request.url.path != "/v1/environments":
            raise NotHandled
        return list_response([env.model_dump(mode="json") for env in environments])

    client = build_fake_anthropic(combine_handlers(list_environments, make_fake_ma_handler(state)))
    await _bind(db_session, tenant.id, "c1", "local")
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        environment_name="open",
    )
    await db_session.commit()

    change = await _isolate(client, db_session_factory, tenant.id, "c1")
    assert change.isolated, "the warning never refuses"
    assert change.network_warning == SEALED_OPEN_NETWORK_WARNING, (
        "its own open environment needs a server admin's confirmation"
    )


async def test_isolating_seals_and_pins_the_channels_own_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(
        tenant.id,
        ("local", False),
        ("shared", False),
        ("daimon", True),
        ("roamer", False),
        ("homebody", False),
    )
    await _bind(db_session, tenant.id, "c1", "local")
    await _bind(db_session, tenant.id, "c2", "shared")
    await _bind(db_session, tenant.id, "c3", "shared")
    await _bind(db_session, tenant.id, "c4", "daimon")
    await _bind(db_session, tenant.id, "c6", "roamer")
    await _bind(db_session, tenant.id, "c8", "homebody")
    await _bind(db_session, tenant.id, "c9", "homebody")
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"roamer": ("c6", "c7"), "homebody": ("c8",)}),
    )
    await db_session.commit()

    async def refusal(channel_id: str) -> str | None:
        try:
            await _isolate(client, db_session_factory, tenant.id, channel_id)
        except ChannelIsolationRefused as exc:
            return exc.reason
        return None

    assert await refusal("c5") == "no_channel_agent", "an unbound channel has no agent of its own"
    assert await refusal("c2") == "shared_channel_agent", "shared answers in c3 too"
    assert await refusal("c4") == "managed_channel_agent", "a built-in agent never belongs"
    assert await refusal("c6") == "pinned_elsewhere", "roamer is pinned to c7 too"
    assert await refusal("c8") == "pinned_shared_channel_agent", "homebody is bound in c9"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == (), "a refusal writes nothing"
    assert await refusal("c1") is None, "c1's own agent answers only there"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert (policy.isolated_channel_ids, policy.sealed_channel_ids) == (("c1",), ("c1",))
    assert policy.agent_channel_pins["local"] == ("c1",), (
        "the own agent is pinned in the same write"
    )

    again = await _isolate(client, db_session_factory, tenant.id, "c1")
    assert (again.changed, again.agent_name) == (False, "local"), "repeating is a no-op"
    ended = await _isolate(client, db_session_factory, tenant.id, "c1", isolated=False)
    assert ended.changed, "ending isolation reports the change"
    assert ended.end_warning == END_ISOLATION_WARNING, "ending warns what stays in place"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == (), "ending isolation drops the marker"
    assert policy.sealed_channel_ids == ("c1",) and "local" in policy.agent_channel_pins, (
        "the seal and the pin stay unless asked"
    )
    await _isolate(client, db_session_factory, tenant.id, "c1")
    lifted = await _isolate(
        client, db_session_factory, tenant.id, "c1", isolated=False, drop_seal_and_pins=True
    )
    assert lifted.end_warning == LIFT_ISOLATION_WARNING, "lifting warns the agents may roam"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.sealed_channel_ids == () and policy.agent_channel_pins == {
        "roamer": ("c6", "c7"),
        "homebody": ("c8",),
    }, "asked, ending drops the seal and the channel's own pins"


async def test_isolating_with_a_fork_pins_the_copy(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("shared", False), ("team-alpha", False))
    await _bind(db_session, tenant.id, "c1", "shared")
    await _bind(db_session, tenant.id, "c2", "shared")
    await db_session.commit()

    with pytest.raises(ChannelIsolationRefused) as refused:
        await _isolate(client, db_session_factory, tenant.id, "c1")
    assert refused.value.reason == "shared_channel_agent", "no copy unless asked"
    change = await _isolate(
        client, db_session_factory, tenant.id, "c1", channel_label="Team Alpha", fork=True
    )

    assert (change.agent_name, change.forked_from) == ("team-alpha-2", "shared"), (
        "copies the agent answering here, uniquely named"
    )
    scope = await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"))
    assert scope is not None and scope.agent_name == "team-alpha-2", "the copy answers in c1"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == ("c1",), "isolated in the same step"
    assert policy.agent_channel_pins == {"team-alpha-2": ("c1",)}, "the copy is pinned, not shared"
    again = await _isolate(client, db_session_factory, tenant.id, "c1", fork=True)
    assert (again.changed, again.forked_from) == (False, None), "repeating never copies twice"

    with pytest.raises(DaimonError, match="pinned to specific channels"):
        await _isolate(
            client, db_session_factory, tenant.id, "c3", fork=True, fork_from="team-alpha-2"
        )


async def test_a_copy_refused_after_the_fork_is_archived(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy starting to answer elsewhere while it is made refuses under the
    lock, and the copy no channel got is archived."""
    tenant = await make_tenant(db_session)
    state = FakeMAState()
    shared = ma_agent(id="agent_0", name="shared", tenant_id=tenant.id)
    state.agents[shared.id] = shared.model_dump(mode="json")
    archived: list[str] = []

    def archive(request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith("/archive"):
            raise NotHandled
        agent_id = request.url.path.split("/")[-2]
        archived.append(agent_id)
        return httpx.Response(200, json=state.agents.pop(agent_id))

    client = build_fake_anthropic(combine_handlers(archive, make_fake_ma_handler(state)))
    await _bind(db_session, tenant.id, "c1", "shared")
    await _bind(db_session, tenant.id, "c2", "shared")
    await db_session.commit()

    async def fork_then_route(*args: Any, **kwargs: Any) -> AgentCopy:
        copy = await fork_agent(*args, **kwargs)
        async with db_session_factory.begin() as session:
            await _bind(session, tenant.id, "c9", kwargs["new_name"])
        return copy

    monkeypatch.setattr(channel_isolation_setup, "fork_agent", fork_then_route)
    with pytest.raises(ChannelIsolationRefused) as refused:
        await _isolate(client, db_session_factory, tenant.id, "c1", fork=True)

    assert refused.value.reason == "shared_channel_agent"
    assert len(archived) == 1, "the copy no channel got is archived"
    scope = await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"))
    assert scope is not None and scope.agent_name == "shared", "the channel keeps its agent"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == ()


async def test_fork_agent_copies_the_source_under_a_new_name(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("shared", False))

    async def fork(new_name: str) -> None:
        await fork_agent(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            source_name="shared",
            new_name=new_name,
            public_url=None,
            subject=ADMIN,
        )

    with pytest.raises(DaimonError, match="Only a workspace or server admin"):
        await fork_agent(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            source_name="shared",
            new_name="nope",
            public_url=None,
            subject=Subject(),
        )
    await fork("team-alpha")
    agents = await list_agents_by_tenant(client, tenant_id=tenant.id)
    names = sorted(agent.metadata[MA_METADATA_KEY_NAME] for agent in agents)
    assert names == ["shared", "team-alpha"], "the copy is tagged with its new name"
    with pytest.raises(DaimonError, match="already exists"):
        await fork("team-alpha")
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"shared": ("C_PINNED",)}),
    )
    await db_session.commit()
    with pytest.raises(DaimonError, match="pinned to specific channels"):
        await fork("team-beta")


async def test_fork_agent_leaves_off_credentialed_servers_and_scoped_skills(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The copy an isolated channel gets holds no token and reaches into no other
    agent's skills; the skills left off are named."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    source = ma_agent(
        id="agent_src",
        name="shared",
        tenant_id=tenant.id,
        mcp_servers=[
            {"type": "url", "name": "crm", "url": "https://crm.example.com/mcp"},
            {"type": "url", "name": "docs", "url": "https://docs.example.com/mcp"},
        ],
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": name,
                "default_config": {
                    "enabled": True,
                    "permission_policy": {"type": "always_allow"},
                },
                "configs": [],
            }
            for name in ("crm", "docs")
        ],
        skills=[
            {"type": "custom", "skill_id": "skill_scoped", "version": "1"},
            {"type": "custom", "skill_id": "skill_library", "version": "1"},
        ],
    )
    state = FakeMAState()
    state.agents[source.id] = source.model_dump(mode="json")
    skills = [
        SkillListResponse(
            id=skill_id,
            type="custom",
            display_title=tenant_scoped_display_title(tenant_id=tenant.id, name=body),
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        for skill_id, body in (("skill_scoped", "shared/notes"), ("skill_library", "notes-lib"))
    ]

    def skills_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/skills":
            return list_response(skills)
        raise NotHandled

    client = build_fake_anthropic(combine_handlers(skills_handler, make_fake_ma_handler(state)))
    await save_agent_mcp_credential(
        sessionmaker=db_session_factory,
        fernet=make_fernet(),
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_src"),
        mcp_server_url="https://crm.example.com/mcp",
        plaintext_token="tok",
    )

    copy = await fork_agent(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        source_name="shared",
        new_name="team-alpha",
        public_url=None,
        subject=ADMIN,
    )

    assert [server.name for server in copy.agent.mcp_servers] == ["docs"], (
        "a server backed by the source's stored token is left off the copy"
    )
    assert [skill.skill_id for skill in copy.agent.skills] == ["skill_library"], (
        "a skill scoped to the source agent is left off; a library skill is kept"
    )
    assert copy.dropped_skills == ("shared/notes",)


def test_a_pinned_agent_is_never_offered_as_a_copy() -> None:
    """A pinned agent can't be copied (`authorize(FORK)`), so its refusals point at its pin."""
    for reason in ("pinned_elsewhere", "pinned_shared_channel_agent"):
        text = render_isolation_refusal(reason, agent_name="roamer")
        assert "Change its pin first" in text, text
        assert "copy" not in text.replace("can't be copied", ""), text
