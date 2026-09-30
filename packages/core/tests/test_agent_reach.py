"""Shell half of agent reach: config rows and thread bindings read from the DB."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.agent_reach import (
    is_agent_local_to_caller,
    load_agent_reach,
    load_target_facts,
    may_bind_as_channel_default,
)
from daimon.core.channel_admins import ChannelAdminCaller
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores import accounts
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.direct_messages import (
    DirectMessageRow,
    delete_conversations_for_account,
    start_conversation,
)
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.task_continuations import cancel_wake_row, record_continuation
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_routine,
    make_tenant,
)
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
        db_session, tenant_id=tenant.id, platform="discord", agent_name="helper", default=DEFAULT
    )
    assert reach.channel_ids == {"c1", "c2"}, "a thread counts as its parent channel"

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
    assert await local(), "the admin of both channels holds it"


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
    assert before.is_reachable_in_tenant and not before.is_local_to_caller_channels, (
        "reachable and not local before a grant"
    )
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


async def test_channel_admin_binds_only_shared_unrouted_or_own_channel_agents(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    for scope, name in (
        (TenantScopeRef(tenant_id=tenant.id), "shared"),
        (ChannelScopeRef(tenant_id=tenant.id, channel_id="b"), "b-agent"),
    ):
        await set_fields(
            db_session, scope=scope, tenant_id=tenant.id, agent_name=name, mode="agent"
        )
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="a",
        role_ids=[],
        user_ids=["u1"],
        actor_account_id=None,
    )
    caller = ChannelAdminCaller(platform_user_id="u1")

    async def may_bind(name: str, *, managed: bool = False, who=caller) -> bool:
        return await may_bind_as_channel_default(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            agent_name=name,
            default=DEFAULT,
            caller=who,
            is_daimon_managed=managed,
        )

    assert not await may_bind("b-agent"), "another channel's own agent never moves in"
    assert await may_bind("b-agent", managed=True), "a managed agent is shared by design"
    assert await may_bind("shared"), "the tenant default is shared by design"
    assert await may_bind("unrouted"), "an agent answering nowhere is free to bind"
    assert await may_bind("b-agent", who=caller.model_copy(update={"is_server_admin": True})), (
        "a server admin binds anything"
    )


async def _admin_of_c1(db_session: AsyncSession, tenant_id) -> None:
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant_id, channel_id="c1"),
        tenant_id=tenant_id,
        agent_name="helper",
        mode="agent",
    )
    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id="c1",
        role_ids=[],
        user_ids=["u1"],
        actor_account_id=None,
    )


async def _is_local(db_session: AsyncSession, tenant_id, agent_name: str = "helper") -> bool:
    return await is_agent_local_to_caller(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        agent_name=agent_name,
        default=DEFAULT,
        caller=ChannelAdminCaller(platform_user_id="u1"),
    )


async def _member(db_session: AsyncSession, tenant, user_id: str, *, admin: bool = False):
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id=user_id, tenant=tenant, account=account
    )
    if admin:
        await accounts.set_role(db_session, account.id, Role.ADMIN)
    return account


async def test_only_a_stronger_requesters_routine_keeps_an_agent_from_a_channel_admin(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    await _member(db_session, tenant, "u5")
    for creator in ("u1", "u5", "u-unknown"):
        await make_routine(
            db_session, tenant=tenant, created_by_user_id=creator, agent_name="helper"
        )
    assert await _is_local(db_session, tenant.id), (
        "the caller's own and plain members' routines keep it local"
    )

    await _member(db_session, tenant, "u7")
    await set_channel_admins(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="c2",
        role_ids=[],
        user_ids=["u7"],
        actor_account_id=None,
    )
    await make_routine(
        db_session, tenant=tenant, created_by_user_id="u7", agent_name="helper", enabled=False
    )
    assert not await _is_local(db_session, tenant.id), (
        "a paused routine by the admin of another channel counts"
    )

    await _member(db_session, tenant, "u9", admin=True)
    await make_routine(db_session, tenant=tenant, created_by_user_id="u9", agent_name="scheduled")
    assert not await may_bind_as_channel_default(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        agent_name="scheduled",
        default=DEFAULT,
        caller=ChannelAdminCaller(platform_user_id="u1"),
        is_daimon_managed=False,
    ), "an agent answering nowhere but running a server admin's routine does not bind"


async def test_only_a_stronger_requesters_timer_keeps_an_agent_from_a_channel_admin(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)

    async def timer(user_id: str, *, admin: bool = False) -> uuid.UUID:
        account = await _member(db_session, tenant, user_id, admin=admin)
        row = await record_continuation(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="c1",
            thread_id="t1",
            requester_account_id=account.id,
            requester_external_user_id=user_id,
            target_ma_agent_id="agent_1",
            target_name="helper",
            reason="timer",
            idempotency_key=uuid.uuid4(),
            requested_work="check in",
            available_at=datetime.now(UTC) + timedelta(hours=1),
        )
        return row.idempotency_key

    await timer("u5")
    assert await _is_local(db_session, tenant.id), "a member's timer keeps it local"
    boss_timer = await timer("u9", admin=True)
    assert not await _is_local(db_session, tenant.id), (
        "a server admin's timer would fire the caller's edits with admin rights"
    )
    assert await cancel_wake_row(db_session, tenant_id=tenant.id, idempotency_key=boss_timer), (
        "the pending timer cancels"
    )
    assert await _is_local(db_session, tenant.id), "a cancelled timer no longer runs"


async def _dm_from(
    db_session: AsyncSession, tenant, *, source_channel_id: str, agent_name: str
) -> uuid.UUID:
    """The rows `start_dm` writes for a /dm started in `source_channel_id`."""
    account = await make_account(db_session, tenant=tenant)
    await start_conversation(
        db_session,
        conversation=DirectMessageRow(
            platform="discord",
            route_key="dm1",
            external_user_id="u5",
            tenant_id=tenant.id,
            account_id=account.id,
            workspace_id="g1",
            channel_id="dm1",
            scope_id="dm:s1",
            source_url=f"https://discord.com/channels/g1/{source_channel_id}",
            source_channel_id=source_channel_id,
            context="",
            memory_read_only=False,
            history=[],
            recent_message_ids=[],
            active_until=None,
        ),
        now=datetime.now(UTC),
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="dm1"),
        tenant_id=tenant.id,
        agent_name=agent_name,
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="dm1",
        thread_id="dm:s1",
        responder_ma_agent_id="agent_1",
        responder_name=agent_name,
        kind="handoff",
    )
    return account.id


async def _dm_agent_channels(db_session: AsyncSession, tenant_id) -> frozenset[str]:
    reach = await load_agent_reach(
        db_session, tenant_id=tenant_id, platform="discord", agent_name="dm-agent", default=DEFAULT
    )
    return reach.channel_ids


async def test_a_dm_counts_as_the_channel_it_was_started_from(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    account_id = await _dm_from(db_session, tenant, source_channel_id="c1", agent_name="dm-agent")

    assert await _dm_agent_channels(db_session, tenant.id) == {"c1"}, (
        "the DM row and scope count as the channel the DM started in"
    )
    assert await _is_local(db_session, tenant.id, "dm-agent"), (
        "a /dm from c1 leaves the agent with c1's admins"
    )
    await delete_conversations_for_account(db_session, account_id=account_id)
    assert await _dm_agent_channels(db_session, tenant.id) == frozenset(), (
        "a DM no longer live answers nowhere"
    )


async def test_a_dm_started_outside_the_callers_channels_is_not_local(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    await _dm_from(db_session, tenant, source_channel_id="c2", agent_name="dm-agent")

    assert await _dm_agent_channels(db_session, tenant.id) == {"c2"}, (
        "the DM counts as c2, where it started"
    )
    assert not await _is_local(db_session, tenant.id, "dm-agent"), (
        "a /dm from c2 keeps the agent from c1's admin"
    )
