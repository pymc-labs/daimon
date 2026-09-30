"""Shell half of agent reach: config rows and thread bindings read from the DB."""

from __future__ import annotations

from daimon.core.agent_reach import (
    is_agent_local_to_caller,
    load_agent_reach,
    load_target_facts,
)
from daimon.core.channel_admins import ChannelAdminCaller
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT = DeploymentDefault(agent_name="daimon")


async def test_reach_and_locality_follow_channels_and_threads(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="helper",
        mode="agent",
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="c2",
        thread_id="t1",
        responder_ma_agent_id="agent_1",
        responder_name="helper",
        kind="handoff",
    )
    reach = await load_agent_reach(
        db_session, tenant_id=tenant.id, agent_name="helper", default=DEFAULT
    )
    assert reach.channel_ids == {"c1", "c2"}

    caller = ChannelAdminCaller(platform_user_id="u1")

    async def local() -> bool:
        return await is_agent_local_to_caller(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            agent_name="helper",
            default=DEFAULT,
            caller=caller,
        )

    assert not await local(), "no grant means no channel admin, whatever the agent"
    for channel in ("c1", "c2"):
        await set_channel_admins(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            channel_id=channel,
            role_ids=[],
            user_ids=["u1"] if channel == "c1" else [],
            actor_account_id=None,
        )
    assert not await local(), "the thread under c2 is outside the caller's channels"
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c2",
        role_ids=[],
        user_ids=["u1"],
        actor_account_id=None,
    )
    assert await local()


async def test_load_target_facts_marks_only_a_channel_admins_local_agent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="helper",
        mode="agent",
    )

    async def facts(caller: ChannelAdminCaller):
        return await load_target_facts(
            db_session,
            "agent_spec_edit",
            tenant_id=tenant.id,
            platform="discord",
            agent_name="helper",
            default=DEFAULT,
            caller=caller,
            is_daimon_managed=False,
        )

    member = ChannelAdminCaller(platform_user_id="u1", role_ids=frozenset({"r1"}))
    before = await facts(member)
    assert before.is_reachable_in_tenant and not before.is_local_to_caller_channels
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c1",
        role_ids=["r1"],
        user_ids=[],
        actor_account_id=None,
    )
    assert (await facts(member)).is_local_to_caller_channels, "a role grant is enough"
    admin = await facts(member.model_copy(update={"is_server_admin": True}))
    assert not admin.is_reachable_in_tenant, "an admin's decision needs no read"
