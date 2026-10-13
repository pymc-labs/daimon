"""Changing Spaces under the policy lock: member rules written, agents kept or refused."""

from __future__ import annotations

import uuid

import pytest
from anthropic import AsyncAnthropic
from daimon.core.access_policy import AgentRule, ChannelRule, SpaceRule, TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.permissions import (
    agent_permissions,
    channel_rule,
    with_agent_rule,
    with_channel_rule,
)
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.space_rules import (
    SpaceChange,
    SpaceRefused,
    add_space_channel,
    create_space,
    delete_space,
    join_refusal,
    remove_space_channel,
)
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.spaces import space_key
from daimon.testing.factories import make_tenant
from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from daimon.testing.ma_models import ma_agent
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ADMIN = Subject(is_admin=True)
OWN = ChannelRule(readers="own", writers="own")
INSIDE = ChannelRule(readers="inside")
DEFAULT = DeploymentDefault(agent_name="daimon")


def _client(tenant_id: uuid.UUID, *agents: tuple[str, bool]) -> AsyncAnthropic:
    """A fake MA holding `agents`, each (name, built in)."""
    state = FakeMAState()
    for index, (name, managed) in enumerate(agents):
        metadata = {MA_METADATA_KEY_MANAGED: "true"} if managed else {}
        agent = ma_agent(id=f"agent_{index}", name=name, tenant_id=tenant_id, metadata=metadata)
        state.agents[agent.id] = agent.model_dump(mode="json")
    return build_fake_anthropic(make_fake_ma_handler(state))


async def _create(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    channel_ids: list[str],
    *,
    rule: ChannelRule = OWN,
    client: AsyncAnthropic | None = None,
) -> SpaceChange:
    return await create_space(
        client or _client(tenant_id),
        factory,
        tenant_id=tenant_id,
        platform="discord",
        name="s",
        rule=rule,
        channel_ids=channel_ids,
        subject=ADMIN,
        default=DEFAULT,
    )


async def _add(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    channel_id: str,
    *,
    client: AsyncAnthropic | None = None,
    extend_agents: bool = False,
) -> SpaceChange:
    return await add_space_channel(
        client or _client(tenant_id),
        factory,
        tenant_id=tenant_id,
        platform="discord",
        name="s",
        channel_id=channel_id,
        subject=ADMIN,
        default=DEFAULT,
        extend_agents=extend_agents,
    )


def _space(rule: ChannelRule) -> SpaceRule:
    return SpaceRule(key=space_key(uuid.uuid4()), name="s", rule=rule, channel_ids=frozenset())


@pytest.mark.parametrize(
    ("member", "space", "lost"),
    [
        (OWN, INSIDE, "readers own to inside"),
        (ChannelRule(readers="inside", writers="none"), INSIDE, "writers none to any"),
        (ChannelRule(writers="none"), OWN, "writers none to own"),
    ],
)
def test_join_refuses_to_loosen_a_members_rule_unless_asked(
    member: ChannelRule, space: ChannelRule, lost: str
) -> None:
    """Joining replaces a channel's rule, so a stricter one is never loosened silently."""
    policy = TenantAccessPolicy(channel_rules={"a": member})
    refusal = join_refusal(policy, _space(space), "a")
    assert refusal is not None and lost in refusal, f"refused, naming {lost}: {refusal}"
    assert join_refusal(policy, _space(space), "a", loosen_rules=True) is None, "unless asked"
    assert join_refusal(TenantAccessPolicy(), _space(space), "a") is None, "an open channel joins"


def test_join_refuses_a_thread() -> None:
    refusal = join_refusal(TenantAccessPolicy(), _space(OWN), "C1:1700000000.000100")
    assert refusal is not None and "thread" in refusal, refusal


async def test_create_writes_the_space_rule_onto_each_member_and_not_the_space(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_rules={"x": AgentRule(runs_in=("c1", "c2"))}),
    )
    await db_session.commit()

    change = await _create(db_session_factory, tenant.id, ["c1", "c2"])

    stored = (
        await db_session.execute(text("SELECT policy FROM tenant_access_policies"))
    ).scalar_one()
    assert "spaces" not in stored, "an older build must still read the row"
    assert stored["channel_rules"] == {
        "c1": {"readers": "own", "writers": "own"},
        "c2": {"readers": "own", "writers": "own"},
    }, "each member's own rule holds the Space's, so an older build keeps it contained"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert agent_permissions(policy, ("x",)).home == space_key(change.space.id), (
        "the agent naming both members has the Space as its home"
    )
    with pytest.raises(SpaceRefused, match="admin"):
        await delete_space(db_session_factory, tenant_id=tenant.id, name="s", subject=Subject())


async def test_space_rule_wins_over_a_member_rule_that_drifted(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _create(db_session_factory, tenant.id, ["c1"])
    loaded = await load_access_policy(db_session, tenant_id=tenant.id)
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=with_channel_rule(loaded, "c1", ChannelRule())
    )

    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert channel_rule(policy, "c1") == OWN, "the Space's rule is the member's, whatever is stored"


async def test_add_extends_the_spaces_agents_and_refuses_a_second_own_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={"c4": OWN},
            agent_rules={"x": AgentRule(runs_in=("c1",)), "y": AgentRule(runs_in=("c4",))},
        ),
    )
    await db_session.commit()
    await _create(db_session_factory, tenant.id, ["c1"])

    change = await _add(db_session_factory, tenant.id, "c3", extend_agents=True)
    assert change.agents == ("x",), "the Space's own agent is extended"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.agent_rules["x"].runs_in == ("c1", "c3"), "x now runs in the new member too"
    with pytest.raises(SpaceRefused, match="keeps its own agents"):
        await _add(db_session_factory, tenant.id, "c4")


async def test_remove_and_delete_leave_members_contained(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_rules={"x": AgentRule(runs_in=("c1", "c2", "c3"))}),
    )
    await db_session.commit()
    await _create(db_session_factory, tenant.id, ["c1", "c2", "c3"])

    removed = await remove_space_channel(
        db_session_factory, tenant_id=tenant.id, name="s", channel_id="c3", subject=ADMIN
    )
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert removed.agents == ("x",) and policy.agent_rules["x"].runs_in == ("c1", "c2"), (
        "the Space's agent drops the channel, keeping the Space as its home"
    )
    assert channel_rule(policy, "c3") == OWN, "the channel that left stays contained"
    assert agent_permissions(policy, ("x",)).home == space_key(removed.space.id), "still the Space"

    deleted = await delete_space(db_session_factory, tenant_id=tenant.id, name="s", subject=ADMIN)
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert deleted.agents == ("x",), "x names two former members: it is reported stranded"
    assert policy.spaces == {} and channel_rule(policy, "c1") == OWN, "members keep the rule"
    assert agent_permissions(policy, ("x",)).home is None, "and x has no home left: runs nowhere"


async def test_join_runs_the_own_agent_checks_on_agents_it_makes_own(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An agent that becomes the Space's own must pass what a channel's own agent does."""
    tenant = await make_tenant(db_session)
    rules = {"builtin": AgentRule(runs_in=("c1",)), "roamer": AgentRule(runs_in=("c2",))}
    await set_access_policy(
        db_session, tenant_id=tenant.id, policy=TenantAccessPolicy(agent_rules=rules)
    )
    # roamer also answers in c9, outside the Space.
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c9"),
        tenant_id=tenant.id,
        agent_name="roamer",
        mode="agent",
    )
    await db_session.commit()
    client = _client(tenant.id, ("builtin", True), ("roamer", False))

    with pytest.raises(SpaceRefused, match="builtin an own agent of Space s: builtin is built in"):
        await _create(db_session_factory, tenant.id, ["c1"], client=client)
    await _create(db_session_factory, tenant.id, ["c3"], client=client)
    with pytest.raises(SpaceRefused, match="roamer also answers outside"):
        await _add(db_session_factory, tenant.id, "c2", client=client)
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert agent_permissions(policy, ("builtin",)).home is None, "nothing changed"
    assert agent_permissions(policy, ("roamer",)).home is None, "nor here"


async def test_remove_and_delete_ask_before_leaving_an_agent_one_channels_own(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """z names only c1 but read across the Space; it would stay c1's own with that memory."""
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_rules={"z": AgentRule(runs_in=("c1",))}),
    )
    await db_session.commit()
    await _create(db_session_factory, tenant.id, ["c1", "c2", "c3"])
    args = {"tenant_id": tenant.id, "name": "s", "subject": ADMIN}

    with pytest.raises(SpaceRefused, match="z would stay c1's own agent"):
        await remove_space_channel(db_session_factory, channel_id="c1", **args)
    removed = await remove_space_channel(
        db_session_factory, channel_id="c1", keep_agents=True, **args
    )
    assert removed.kept == ("z",), "z is reported kept"
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert agent_permissions(policy, ("z",)).home == "c1", "z is c1's own agent now"

    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=with_agent_rule(policy, "z", AgentRule(runs_in=("c2",))),
    )
    await db_session.commit()
    with pytest.raises(SpaceRefused, match="z would stay its channel's own agent"):
        await delete_space(db_session_factory, **args)
    deleted = await delete_space(db_session_factory, keep_agents=True, **args)
    assert deleted.kept == ("z",) and deleted.agents == (), "kept, not stranded"
