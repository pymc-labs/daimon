"""Turning channel isolation on and off, and the fork that gives a channel its own agent."""

from __future__ import annotations

import uuid

import pytest
from anthropic import AsyncAnthropic
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_fork import fork_agent
from daimon.core.channel_isolation_setup import (
    ChannelIsolationRefused,
    isolated_agent_name,
    set_channel_isolation,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.errors import DaimonError
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


async def test_isolating_needs_the_channels_own_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, _ = _client(tenant.id, ("local", False), ("shared", False), ("daimon", True))
    await _bind(db_session, tenant.id, "c1", "local")
    await _bind(db_session, tenant.id, "c2", "shared")
    await _bind(db_session, tenant.id, "c3", "shared")
    await _bind(db_session, tenant.id, "c4", "daimon")

    async def isolate(channel_id: str) -> str | None:
        try:
            await set_channel_isolation(
                client,
                db_session_factory,
                tenant_id=tenant.id,
                channel_id=channel_id,
                isolated=True,
                default=DEFAULT,
                actor_account_id=None,
            )
        except ChannelIsolationRefused as exc:
            return exc.reason
        return None

    assert await isolate("c5") == "no_channel_agent", "an unbound channel has no agent of its own"
    assert await isolate("c2") == "shared_channel_agent", "shared answers in c3 too"
    assert await isolate("c4") == "managed_channel_agent", "a built-in agent never belongs"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == (), "a refusal writes nothing"
    assert await isolate("c1") is None, "c1's own agent answers only there"
    assert (await load_access_policy(db_session, tenant_id=tenant.id)).isolated_channel_ids == (
        "c1",
    )

    again = await set_channel_isolation(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        channel_id="c1",
        isolated=True,
        default=DEFAULT,
        actor_account_id=None,
    )
    assert (again.changed, again.agent_name) == (False, "local"), "repeating is a no-op"
    ended = await set_channel_isolation(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        channel_id="c1",
        isolated=False,
        default=DEFAULT,
        actor_account_id=None,
    )
    assert ended.changed, "ending isolation reports the change"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == (), "ending isolation clears the id"


async def test_isolating_with_a_fork_binds_the_copy(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client, state = _client(tenant.id, ("shared", False), ("team-alpha", False))
    await _bind(db_session, tenant.id, "c1", "shared")
    await _bind(db_session, tenant.id, "c2", "shared")
    forks: list[tuple[str, str]] = []

    async def fork(source: str, new_name: str) -> None:
        forks.append((source, new_name))
        agent = ma_agent(id="agent_fork", name=new_name, tenant_id=tenant.id)
        state.agents[agent.id] = agent.model_dump(mode="json")

    change = await set_channel_isolation(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        channel_id="c1",
        isolated=True,
        default=DEFAULT,
        actor_account_id=None,
        channel_label="Team Alpha",
        fork=fork,
    )

    assert forks == [("shared", "team-alpha-2")], "copies the agent answering here, uniquely named"
    assert (change.agent_name, change.forked_from) == ("team-alpha-2", "shared")
    scope = await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"))
    assert scope is not None and scope.agent_name == "team-alpha-2", "the copy answers in c1"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == ("c1",), "isolated in the same step"

    with pytest.raises(ChannelIsolationRefused) as refused:
        await set_channel_isolation(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            channel_id="c3",
            isolated=True,
            default=DEFAULT,
            actor_account_id=None,
            fork=fork,
            fork_from="team-alpha-2",
        )
    assert refused.value.reason == "agent_confined", "another channel's own agent isn't copied"


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
