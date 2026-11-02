"""Channel setting changes: authorized, compared against what the caller read, recorded."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from anthropic import AsyncAnthropic
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.channel_budget import parse_budget_spec
from daimon.core.channel_changes import (
    BudgetValue,
    ChannelActor,
    ChannelChangeRefused,
    Expected,
    clear_budget,
    clear_default_agent,
    keep_agent,
    raise_budget,
    release_agent,
    set_access,
    set_budget,
    set_default_agent,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.errors import StaleChannelValue
from daimon.core.permissions import runs_only_in
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.channel_changes import list_changes
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT = DeploymentDefault(agent_name="daimon")


def _actor(tenant_id: uuid.UUID, subject: Subject | None = None) -> ChannelActor:
    return ChannelActor(
        tenant_id=tenant_id,
        account_id=None,
        subject=subject or Subject(is_admin=True),
        surface="web",
    )


def _client(tenant_id: uuid.UUID, *names: str) -> AsyncAnthropic:
    state = FakeMAState()
    for index, name in enumerate(names):
        metadata = {MA_METADATA_KEY_MANAGED: "true"} if name == "daimon" else {}
        agent = ma_agent(id=f"agent_{index}", name=name, tenant_id=tenant_id, metadata=metadata)
        state.agents[agent.id] = agent.model_dump(mode="json")
    return build_fake_anthropic(make_fake_ma_handler(state))


async def _bind(session: AsyncSession, tenant_id: uuid.UUID, channel_id: str, agent: str) -> None:
    await set_fields(
        session,
        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
        tenant_id=tenant_id,
        agent_name=agent,
        mode="agent",
    )


async def _settings(session: AsyncSession, tenant_id: uuid.UUID, channel_id: str) -> list[str]:
    changes = await list_changes(session, tenant_id=tenant_id, channel_id=channel_id)
    return [change.setting for change in changes]


async def test_only_an_admin_changes_a_channel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    member = _actor(tenant.id, Subject())
    agent_key = _actor(tenant.id, Subject(is_admin=True, via_agent_key=True))
    for actor in (member, agent_key):
        with pytest.raises(ChannelChangeRefused) as default:
            await set_default_agent(
                db_session_factory, actor, platform="discord", channel_id="c1", agent_name="local"
            )
        assert default.value.kind == "forbidden", "an agent key never counts as an admin here"
    with pytest.raises(ChannelChangeRefused) as budget:
        await clear_budget(db_session_factory, member, platform="discord", channel_id="c1")
    assert budget.value.kind == "forbidden"
    assert await _settings(db_session, tenant.id, "c1") == [], "a refusal records nothing"


async def test_default_agent_is_compared_written_and_recorded(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = _actor(tenant.id)

    change = await set_default_agent(
        db_session_factory,
        actor,
        platform="discord",
        channel_id="c1",
        agent_name="local",
        expected=Expected(None),
    )
    assert (change.before, change.after) == (None, "local")
    with pytest.raises(StaleChannelValue) as stale:
        await set_default_agent(
            db_session_factory,
            actor,
            platform="discord",
            channel_id="c1",
            agent_name="other",
            expected=Expected(None),
        )
    assert stale.value.current == "local", "the stale error carries the value now"
    await clear_default_agent(
        db_session_factory, actor, platform="discord", channel_id="c1", expected=Expected("local")
    )

    scope = await get_scope(db_session, scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"))
    assert scope is None or scope.agent_name is None, "the clear is written"
    changes = await list_changes(db_session, tenant_id=tenant.id, channel_id="c1")
    assert [(c.before, c.after, c.surface) for c in changes] == [
        ("local", None, "web"),
        (None, "local", "web"),
    ], "newest first; the stale attempt left no row"


async def test_a_default_the_rules_would_refuse_is_invalid(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            channel_rules={"c2": ChannelRule(readers="own", writers="own")},
            agent_rules={"homebody": AgentRule(runs_in=("c2",))},
        ),
    )
    await _bind(db_session, tenant.id, "c2", "homebody")
    await db_session.commit()
    actor = _actor(tenant.id)

    with pytest.raises(ChannelChangeRefused) as elsewhere:
        await set_default_agent(
            db_session_factory, actor, platform="discord", channel_id="c1", agent_name="homebody"
        )
    assert (elsewhere.value.kind, elsewhere.value.reason) == ("invalid", "agent_has_home")
    with pytest.raises(ChannelChangeRefused) as keeps_own:
        await clear_default_agent(db_session_factory, actor, platform="discord", channel_id="c2")
    assert keeps_own.value.reason == "keeps_own", "an own-agents channel keeps its agent"


async def test_budget_set_raise_and_clear_are_recorded_and_compared(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = _actor(tenant.id)
    spec = parse_budget_spec(limit_usd="10", window="monthly", starts_at=None, ends_at=None)

    with pytest.raises(ChannelChangeRefused) as missing:
        await raise_budget(
            db_session_factory, actor, platform="discord", channel_id="c1", amount_usd=Decimal(5)
        )
    assert missing.value.kind == "not_found"
    row = await set_budget(
        db_session_factory,
        actor,
        platform="discord",
        channel_id="c1",
        spec=spec,
        expected=Expected(None),
    )
    current = BudgetValue.of(row)
    with pytest.raises(StaleChannelValue):
        await set_budget(
            db_session_factory,
            actor,
            platform="discord",
            channel_id="c1",
            spec=spec,
            expected=Expected(None),
        )
    raised = await raise_budget(
        db_session_factory,
        actor,
        platform="discord",
        channel_id="c1",
        amount_usd=Decimal(5),
        expected=Expected(current),
    )
    assert raised.limit_usd == Decimal(15)
    assert await clear_budget(db_session_factory, actor, platform="discord", channel_id="c1")

    changes = await list_changes(db_session, tenant_id=tenant.id, channel_id="c1")
    assert [(c.before, c.after) for c in reversed(changes)] == [
        (None, {"limit_usd": "10.00", "window": "monthly", "starts_at": None, "ends_at": None}),
        (
            {"limit_usd": "10.00", "window": "monthly", "starts_at": None, "ends_at": None},
            {"limit_usd": "15.00", "window": "monthly", "starts_at": None, "ends_at": None},
        ),
        ({"limit_usd": "15.00", "window": "monthly", "starts_at": None, "ends_at": None}, None),
    ]


async def test_access_change_keeps_the_channels_agent_and_records_both(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client = _client(tenant.id, "local", "daimon")
    await _bind(db_session, tenant.id, "c1", "local")
    await db_session.commit()
    actor = _actor(tenant.id)

    with pytest.raises(StaleChannelValue):
        await set_access(
            client,
            db_session_factory,
            actor,
            platform="discord",
            channel_id="c1",
            readers="own",
            writers=None,
            default=DEFAULT,
            expected=Expected(ChannelRule(readers="inside")),
        )
    change = await set_access(
        client,
        db_session_factory,
        actor,
        platform="discord",
        channel_id="c1",
        readers="own",
        writers=None,
        default=DEFAULT,
        expected=Expected(ChannelRule()),
    )
    assert change.agent_name == "local", "the channel's own agent is kept there"
    changes = await list_changes(db_session, tenant_id=tenant.id, channel_id="c1")
    assert {c.setting: (c.before, c.after) for c in changes} == {
        "access": (
            {"readers": "any", "writers": "any"},
            {"readers": "own", "writers": "own"},
        ),
        "kept_agents": ([], ["local"]),
    }


async def test_keep_and_release_an_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    client = _client(tenant.id, "local")
    await db_session.commit()
    actor = _actor(tenant.id)

    await keep_agent(
        client,
        db_session_factory,
        actor,
        platform="discord",
        channel_id="c1",
        agent_name="local",
        default=DEFAULT,
        expected=Expected(()),
    )
    with pytest.raises(StaleChannelValue):
        await release_agent(
            client,
            db_session_factory,
            actor,
            platform="discord",
            channel_id="c1",
            agent_name="local",
            default=DEFAULT,
            expected=Expected(()),
        )
    await release_agent(
        client,
        db_session_factory,
        actor,
        platform="discord",
        channel_id="c1",
        agent_name="local",
        default=DEFAULT,
        expected=Expected(("local",)),
    )
    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert runs_only_in(policy, "c1") == ()
    changes = await list_changes(db_session, tenant_id=tenant.id, channel_id="c1")
    assert [(c.setting, c.before, c.after) for c in reversed(changes)] == [
        ("kept_agents", [], ["local"]),
        ("kept_agents", ["local"], []),
    ]
