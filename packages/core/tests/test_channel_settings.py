"""A channel's further settings: authorized, compared and recorded through one service."""

from __future__ import annotations

import uuid

import pytest
from anthropic.types.beta import BetaCloudConfig, BetaLimitedNetwork
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelActor, ChannelChangeRefused, Expected, set_access
from daimon.core.channel_settings import (
    ConfirmNeeded,
    clear_provider_pin,
    clear_thread_override,
    list_thread_overrides,
    set_environment,
    set_tool_rule,
    set_workspace_default_agent,
)
from daimon.core.errors import StaleChannelValue
from daimon.core.permissions import with_agent_rule, with_channel_rule
from daimon.core.scope import DeploymentDefault
from daimon.core.scope_cascade import CascadeMode, CascadeScope
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.stores.switch_modes import set_switch_mode
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
from mux.testing.drivers.anthropic.ma_models import EMPTY_CLOUD_CONFIG
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT = DeploymentDefault(agent_name="daimon")


async def _actor(db: AsyncSession, *, admin: bool = True) -> ChannelActor:
    tenant = await make_tenant(db)
    account = await make_account(db, tenant=tenant)
    await db.commit()
    return ChannelActor(
        tenant_id=tenant.id,
        account_id=account.id,
        subject=Subject(is_admin=admin),
        surface="web",
    )


def _environments(tenant_id: uuid.UUID) -> MARouter:
    router = MARouter()
    router.add_environment_list(
        ma_environment(id="env_open", name="open", tenant_id=tenant_id),
        ma_environment(
            id="env_sealed",
            name="sealed",
            tenant_id=tenant_id,
            config=BetaCloudConfig(
                type="cloud",
                networking=BetaLimitedNetwork(
                    type="limited",
                    allowed_hosts=[],
                    allow_package_managers=False,
                    allow_mcp_servers=False,
                ),
                packages=EMPTY_CLOUD_CONFIG.packages,
            ),
        ),
    )
    return router


async def test_a_sandbox_pick_is_compared_confirmed_and_recorded(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    client = build_fake_anthropic(_environments(actor.tenant_id).dispatch)

    async def pick(name: str | None, expected: str | None, **kw: bool) -> str | None:
        change = await set_environment(
            client,
            db_session_factory,
            actor,
            platform="discord",
            channel_id="c1",
            environment_name=name,
            default=DEFAULT,
            expected=Expected(expected),
            **kw,
        )
        return change.after

    with pytest.raises(ChannelChangeRefused) as missing:
        await pick("nope", None)
    assert missing.value.kind == "not_found"
    assert await pick("open", None) == "open"
    with pytest.raises(StaleChannelValue):
        await pick("sealed", None)
    await set_access_policy(
        db_session,
        tenant_id=actor.tenant_id,
        policy=with_channel_rule(TenantAccessPolicy(), "c1", ChannelRule(readers="inside")),
    )
    await db_session.commit()
    await pick("sealed", "open")
    with pytest.raises(ConfirmNeeded):
        await pick("open", "sealed")
    assert await pick("open", "sealed", confirm_open_network=True) == "open"
    records = await list_changes(db_session, tenant_id=actor.tenant_id, channel_id="c1")
    assert [(r.setting, r.after, r.surface) for r in records] == [
        ("environment", "open", "web"),
        ("environment", "sealed", "web"),
        ("environment", "open", "web"),
    ]


async def test_a_member_cannot_pick_a_sandbox(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, admin=False)
    client = build_fake_anthropic(_environments(actor.tenant_id).dispatch)
    with pytest.raises(ChannelChangeRefused) as refused:
        await set_environment(
            client,
            db_session_factory,
            actor,
            platform="discord",
            channel_id=None,
            environment_name="open",
            default=DEFAULT,
        )
    assert refused.value.kind == "forbidden"


async def test_the_workspace_default_agent_records_under_the_workspace(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    change = await set_workspace_default_agent(
        db_session_factory, actor, platform="slack", agent_name="helper", expected=Expected(None)
    )
    assert (change.before, change.after) == (None, "helper")
    with pytest.raises(StaleChannelValue):
        await set_workspace_default_agent(
            db_session_factory, actor, platform="slack", agent_name=None, expected=Expected(None)
        )
    [record] = await list_changes(db_session, tenant_id=actor.tenant_id, channel_id=WORKSPACE_ID)
    assert (record.setting, record.after) == ("default_agent", "helper")
    member = ChannelActor(
        tenant_id=actor.tenant_id, account_id=None, subject=Subject(), surface="web"
    )
    with pytest.raises(ChannelChangeRefused):
        await set_workspace_default_agent(
            db_session_factory, member, platform="slack", agent_name=None
        )


async def test_thread_overrides_list_under_their_channel_and_clear_one_by_one(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    tenant_id = actor.tenant_id
    await set_switch_mode(
        db_session,
        "progress_plans",
        tenant_id=tenant_id,
        platform="slack",
        scope=CascadeScope.THREAD,
        scope_id="C1:171.1",
        mode=CascadeMode.ON,
        parent_id="C1",
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant_id,
        policy=with_channel_rule(TenantAccessPolicy(), "C1:171.2", ChannelRule(readers="inside")),
    )
    await create_binding(
        db_session,
        tenant_id=tenant_id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="C1:171.3",
        responder_ma_agent_id="agent_1",
        responder_name="helper",
        kind="handoff",
    )
    await db_session.commit()
    listed = await list_thread_overrides(
        db_session, tenant_id=tenant_id, platform="slack", channel_id="C1"
    )
    assert [(o.thread_id, o.kind) for o in listed] == [
        ("C1:171.1", "progress_plans"),
        ("C1:171.2", "thread_rule"),
        ("C1:171.3", "handoff"),
    ]
    with pytest.raises(StaleChannelValue):
        await clear_thread_override(
            db_session_factory,
            actor,
            platform="slack",
            channel_id="C1",
            thread_id="C1:171.9",
            kind="progress_plans",
        )
    for override in listed:
        await clear_thread_override(
            db_session_factory,
            actor,
            platform="slack",
            channel_id="C1",
            thread_id=override.thread_id,
            kind=override.kind,
        )
    assert (
        await list_thread_overrides(
            db_session, tenant_id=tenant_id, platform="slack", channel_id="C1"
        )
        == []
    )
    records = await list_changes(db_session, tenant_id=tenant_id, channel_id="C1")
    assert sorted(r.setting for r in records) == [
        "thread_handoff",
        "thread_progress_plans",
        "thread_rule",
    ]


async def test_tool_rules_need_tool_safety_and_a_real_tool(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    agent = ma_agent(name="helper", tenant_id=actor.tenant_id)

    async def put(*, tool_safety: bool, tool_key: str) -> None:
        await set_tool_rule(
            db_session_factory,
            actor,
            tool_safety=tool_safety,
            channel_id="c1",
            agent=agent,
            tool_key=tool_key,
            rule="deny",
        )

    with pytest.raises(ChannelChangeRefused) as off:
        await put(tool_safety=False, tool_key="docs/search")
    assert off.value.reason == "tool_safety_off"
    with pytest.raises(ChannelChangeRefused) as bad:
        await put(tool_safety=True, tool_key="docs/search")
    assert bad.value.reason == "bad_tool"


async def test_a_provider_pin_that_is_gone_is_stale(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    with pytest.raises(StaleChannelValue):
        await clear_provider_pin(
            db_session_factory, actor, platform="discord", channel_id="c1", expected_local=1
        )


async def test_leaving_own_needs_a_choice_for_its_agents(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    policy = with_agent_rule(
        with_channel_rule(TenantAccessPolicy(), "c1", ChannelRule(readers="own", writers="own")),
        "helper",
        AgentRule(runs_in=("c1",)),
    )
    await set_access_policy(db_session, tenant_id=actor.tenant_id, policy=policy)
    await db_session.commit()
    client = build_fake_anthropic(MARouter().dispatch)

    async def leave(**kw: bool) -> None:
        await set_access(
            client,
            db_session_factory,
            actor,
            platform="discord",
            channel_id="c1",
            readers="any",
            writers=None,
            default=DEFAULT,
            **kw,
        )

    for flags in ({}, {"release_agents": True, "keep_rules": True}):
        with pytest.raises(ChannelChangeRefused) as refused:
            await leave(**flags)
        assert refused.value.reason == "choose"
    await leave(keep_rules=True)
    kept = await load_access_policy(db_session, tenant_id=actor.tenant_id)
    assert kept.agent_rules["helper"].runs_in == ("c1",), "keep leaves the agent's rule"
