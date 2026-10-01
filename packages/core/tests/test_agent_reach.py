"""Shell half of agent reach: config rows and thread bindings read from the DB."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.agent_reach import (
    is_agent_local_to_caller,
    load_agent_reach,
    load_target_facts,
    may_bind_as_channel_default,
)
from daimon.core.channel_admins import ChannelAdminCaller
from daimon.core.operation_policy import OperationKind
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef, UserScopeRef
from daimon.core.stores import accounts
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.credential_requests import create_credential_request
from daimon.core.stores.direct_messages import (
    DirectMessageRow,
    delete_conversations_for_account,
    start_conversation,
)
from daimon.core.stores.domain import ContinuationReason, Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.task_continuations import (
    cancel_wake_row,
    claim_continuation,
    record_continuation,
    settle_continuation,
)
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_routine,
    make_tenant,
    make_thread_session,
    make_usage_event,
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
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        agent_names=("helper",),
        ma_agent_id=None,
        default=DEFAULT,
    )
    assert reach.channel_ids == {"c1", "c2"}, "a thread counts as its parent channel"

    caller = ChannelAdminCaller(platform_user_id="u1")

    async def local() -> bool:
        return await is_agent_local_to_caller(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            agent_names=("helper",),
            ma_agent_id=None,
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
            agent_names=("helper",),
            ma_agent_id=None,
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
            agent_names=(name,),
            ma_agent_id=None,
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
        agent_names=(agent_name,),
        ma_agent_id=None,
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
            db_session,
            tenant=tenant,
            created_by_user_id=creator,
            agent_name="helper",
            channel_id="c1",
        )
    assert await _is_local(db_session, tenant.id), (
        "the caller's own and plain members' routines in c1 keep it local"
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
        db_session,
        tenant=tenant,
        created_by_user_id="u7",
        agent_name="helper",
        enabled=False,
        channel_id="c1",
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
        agent_names=("scheduled",),
        ma_agent_id=None,
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


@pytest.mark.parametrize("reason", ["task_handoff", "private_input_applied"])
async def test_a_stronger_requesters_claimed_wake_counts_until_it_settles(
    db_session: AsyncSession, reason: ContinuationReason
) -> None:
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    account = await _member(db_session, tenant, "u9", admin=True)
    key = uuid.uuid4()
    await record_continuation(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="c1",
        thread_id="t1",
        requester_account_id=account.id,
        requester_external_user_id="u9",
        target_ma_agent_id="agent_1",
        target_name="helper",
        reason=reason,
        idempotency_key=key,
    )
    assert not await _is_local(db_session, tenant.id), f"a pending {reason} runs with admin rights"
    now = datetime.now(UTC)
    assert await claim_continuation(db_session, idempotency_key=key, now=now), "the wake claims"
    assert not await _is_local(db_session, tenant.id), f"a claimed {reason} still runs"
    await settle_continuation(db_session, idempotency_key=key, status="delivered", now=now)
    assert await _is_local(db_session, tenant.id), f"a delivered {reason} no longer runs"


async def test_a_stronger_requesters_private_input_counts_against_the_agent_that_asked(
    db_session: AsyncSession,
) -> None:
    """An applied input may resume the agent that asked for it, not only the key's agent."""
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    boss = await _member(db_session, tenant, "u9", admin=True)
    await make_thread_session(
        db_session,
        tenant=tenant,
        account=boss,
        thread_id="t1",
        ma_agent_id="agent_1",
        channel_id="c1",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )

    async def local() -> bool:
        return await is_agent_local_to_caller(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            agent_names=("helper",),
            ma_agent_id="agent_1",
            default=DEFAULT,
            caller=ChannelAdminCaller(platform_user_id="u1"),
        )

    assert await local(), "a session in c1 alone keeps helper with c1's admin"
    key = uuid.uuid4()
    await create_credential_request(
        db_session,
        token=f"tok_{uuid.uuid4()}",
        kind="env",
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=boss.id,
        target="API_KEY",
        mcp_server_url=None,
        requester_platform_user_id="u9",
        channel_id="t1",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        idempotency_key=key,
        target_ma_agent_id="agent_key_owner",
        target_name="key-owner",
        requested_work="finish wiring the key",
        platform="discord",
        parent_channel_id="c1",
        origin_thread_id="t1",
    )
    await record_continuation(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="c1",
        thread_id="t1",
        requester_account_id=boss.id,
        requester_external_user_id="u9",
        target_ma_agent_id="agent_key_owner",
        target_name="key-owner",
        reason="private_input_applied",
        idempotency_key=key,
    )
    assert not await local(), "a server admin's input asked by helper would resume it as them"


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
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        agent_names=("dm-agent",),
        ma_agent_id=None,
        default=DEFAULT,
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


async def _key_facts(db_session: AsyncSession, tenant_id, operation="key_replace", **target):
    return await load_target_facts(
        db_session,
        operation,
        tenant_id=tenant_id,
        platform="discord",
        agent_names=target.get("names", ("Helper", "helper")),
        ma_agent_id=target.get("ma_agent_id", "agent_1"),
        default=DEFAULT,
        caller=ChannelAdminCaller(platform_user_id="u1"),
        is_daimon_managed=False,
        caller_platform_user_id="u1",
    )


async def test_locality_counts_every_name_the_agent_carries(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    assert (await _key_facts(db_session, tenant.id)).is_local_to_caller_channels, (
        "a default in c1 under its routing name stays with c1's admin"
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c2"),
        tenant_id=tenant.id,
        agent_name="Helper",
        mode="agent",
    )
    facts = await _key_facts(db_session, tenant.id)
    assert facts.is_reachable_in_tenant and not facts.is_local_to_caller_channels, (
        "a default in c2 under its display name takes it out of c1's admin's hands"
    )


async def test_a_personal_default_is_shared_for_key_changes_and_never_local(
    db_session: AsyncSession,
) -> None:
    """The wide sharing read finds a personal default; locality must not override it."""
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    account = await make_account(db_session, tenant=tenant)
    await set_fields(
        db_session,
        scope=UserScopeRef(account_id=account.id),
        tenant_id=tenant.id,
        agent_name="solo",
    )
    key = await _key_facts(db_session, tenant.id, names=("solo",), ma_agent_id="agent_solo")
    assert key.is_reachable_in_tenant and not key.is_local_to_caller_channels, (
        "someone's personal default answers them everywhere"
    )
    spec = await _key_facts(
        db_session, tenant.id, operation="agent_spec_edit", names=("solo",), ma_agent_id=None
    )
    assert not spec.is_reachable_in_tenant, "a spec edit keeps the cascade-only read"
    assert not await _is_local(db_session, tenant.id, "solo"), "not local for binding either"


async def test_bindings_and_routines_count_by_the_agents_stable_id(
    db_session: AsyncSession,
) -> None:
    """A rename since the thread was bound or the routine was made cannot hide either."""
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="c2",
        thread_id="t9",
        responder_ma_agent_id="agent_1",
        responder_name="old-name",
        kind="handoff",
    )
    assert not (await _key_facts(db_session, tenant.id)).is_local_to_caller_channels, (
        "a thread bound under an old name still answers in c2"
    )
    assert (
        await _key_facts(db_session, tenant.id, ma_agent_id="agent_other")
    ).is_local_to_caller_channels, "another agent's binding does not count"

    other = await make_tenant(db_session)
    await _admin_of_c1(db_session, other.id)
    await _member(db_session, other, "u9", admin=True)
    await make_routine(
        db_session, tenant=other, created_by_user_id="u9", agent_id="agent_1", agent_name="old"
    )
    facts = await _key_facts(db_session, other.id)
    assert not facts.is_local_to_caller_channels and facts.runs_unattended_beyond_caller, (
        "a server admin's routine on the same agent under an old name holds it back"
    )


async def _grant(db_session: AsyncSession, tenant_id, channel_id: str, user_id: str) -> None:
    await set_channel_admins(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id=channel_id,
        role_ids=[],
        user_ids=[user_id],
        actor_account_id=None,
    )


async def _unrouted_key_facts(db_session: AsyncSession, tenant_id, user_id: str, account_id=None):
    """A key change on `unrouted` (no default, no bound thread) by `user_id`."""
    return await load_target_facts(
        db_session,
        "key_replace",
        tenant_id=tenant_id,
        platform="discord",
        agent_names=("unrouted",),
        ma_agent_id="agent_x",
        default=DEFAULT,
        caller=ChannelAdminCaller(platform_user_id=user_id),
        is_daimon_managed=False,
        caller_account_id=account_id,
        caller_platform_user_id=user_id,
    )


async def test_another_accounts_live_session_counts_by_its_channel(
    db_session: AsyncSession,
) -> None:
    """An agent answering nowhere can still run in other members' live sessions."""
    tenant = await make_tenant(db_session)
    await _grant(db_session, tenant.id, "c2", "u2")
    await _grant(db_session, tenant.id, "c9", "u9")
    member = await make_account(db_session, tenant=tenant)
    live = await make_thread_session(
        db_session,
        tenant=tenant,
        account=member,
        thread_id="t1",
        ma_agent_id="agent_x",
        channel_id="c2",
    )
    assert (await _unrouted_key_facts(db_session, tenant.id, "u2")).is_local_to_caller_channels, (
        "c2's admin holds the agent whose only run is recorded in c2"
    )
    own = await _unrouted_key_facts(db_session, tenant.id, "u5", account_id=member.id)
    assert not own.is_reachable_in_tenant, "the session's own member reaches nobody else"

    dormant = await make_thread_session(
        db_session,
        tenant=tenant,
        account=await make_account(db_session, tenant=tenant),
        thread_id="t2",
        ma_agent_id="agent_x",
    )
    unknown = await _unrouted_key_facts(db_session, tenant.id, "u2")
    assert unknown.is_reachable_in_tenant and not unknown.is_local_to_caller_channels, (
        "a second session whose channel is unknown could run anywhere"
    )
    assert unknown.has_unplaced_run, "the unknown channel is the reason the refusal names"

    await make_usage_event(
        db_session, tenant=tenant, managed_session_id=dormant.ma_session_id, channel_id="c2"
    )
    assert (await _unrouted_key_facts(db_session, tenant.id, "u2")).is_local_to_caller_channels, (
        "a session with no recorded channel is placed by its spend"
    )
    elsewhere = await _unrouted_key_facts(db_session, tenant.id, "u9")
    assert elsewhere.is_reachable_in_tenant and not elsewhere.is_local_to_caller_channels, (
        "the admin of an unrelated channel never takes over a session running in c2"
    )
    assert not elsewhere.has_unplaced_run, "every run is placed, so that is not the reason"
    plain = await _unrouted_key_facts(db_session, tenant.id, "u5")
    assert plain.is_reachable_in_tenant and not plain.is_local_to_caller_channels, (
        "a plain member stays refused"
    )
    await make_usage_event(
        db_session, tenant=tenant, managed_session_id=live.ma_session_id, channel_id="c3"
    )
    assert not (
        await _unrouted_key_facts(db_session, tenant.id, "u2")
    ).is_local_to_caller_channels, "spend in c3 counts beside the channel recorded at creation"


async def test_other_peoples_routines_count_by_their_channel(db_session: AsyncSession) -> None:
    """A plain member's routine running an agent into c2 keeps it from c9's admin."""
    tenant = await make_tenant(db_session)
    await _grant(db_session, tenant.id, "c2", "u2")
    await _grant(db_session, tenant.id, "c9", "u9")
    await _member(db_session, tenant, "u5")
    await make_routine(
        db_session,
        tenant=tenant,
        created_by_user_id="u5",
        agent_id="agent_x",
        agent_name="unrouted",
        channel_id="c2",
    )

    elsewhere = await _unrouted_key_facts(db_session, tenant.id, "u9")
    assert elsewhere.is_reachable_in_tenant and not elsewhere.is_local_to_caller_channels, (
        "a routine into c2 is outside c9's admin's channels"
    )
    assert (await _unrouted_key_facts(db_session, tenant.id, "u2")).is_local_to_caller_channels, (
        "a plain member's routine into c2 leaves the agent with c2's admin"
    )
    plain = await _unrouted_key_facts(db_session, tenant.id, "u5")
    assert not plain.is_reachable_in_tenant, "the creator's own routine is nobody else's reach"

    await make_routine(
        db_session, tenant=tenant, created_by_user_id="u5", agent_id="agent_x", agent_name="x"
    )
    unknown = await _unrouted_key_facts(db_session, tenant.id, "u2")
    assert not unknown.is_local_to_caller_channels, "a routine with no channel could run anywhere"


async def test_an_agent_answering_nowhere_is_local_to_no_channel_admin(
    db_session: AsyncSession,
) -> None:
    """Locality never widens the sharing read; binding such an agent stays open."""
    tenant = await make_tenant(db_session)
    await _grant(db_session, tenant.id, "c9", "u9")
    facts = await _unrouted_key_facts(db_session, tenant.id, "u9")
    assert not facts.is_reachable_in_tenant, "an agent no one reaches is open to anyone"
    assert not facts.is_local_to_caller_channels, "and local to no channel admin"
    assert not await is_agent_local_to_caller(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        agent_names=("unrouted",),
        ma_agent_id="agent_x",
        default=DEFAULT,
        caller=ChannelAdminCaller(platform_user_id="u9"),
    ), "answering nowhere is not local"
    assert await may_bind_as_channel_default(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        agent_names=("unrouted",),
        ma_agent_id="agent_x",
        default=DEFAULT,
        caller=ChannelAdminCaller(platform_user_id="u9"),
        is_daimon_managed=False,
    ), "a channel admin still binds an agent answering nowhere yet"


async def test_a_key_change_without_a_stable_id_is_local_to_nobody(
    db_session: AsyncSession,
) -> None:
    """With no id to find sessions by, the sharing read fails closed and so does locality."""
    tenant = await make_tenant(db_session)
    await _admin_of_c1(db_session, tenant.id)
    facts = await _key_facts(db_session, tenant.id, ma_agent_id=None)
    assert facts.is_reachable_in_tenant and not facts.is_local_to_caller_channels, (
        "a key change that cannot see live sessions is never a channel admin's"
    )
    spec = await _key_facts(db_session, tenant.id, operation="agent_spec_edit", ma_agent_id=None)
    assert spec.is_local_to_caller_channels, "a spec edit reads the cascade and stays local"


@pytest.mark.parametrize("reach", ["personal_default", "thread_binding", "routine", "live_session"])
async def test_a_skill_repo_connect_reads_sharing_as_wide_as_a_key_change(
    db_session: AsyncSession, reach: str
) -> None:
    """Skills reach every place keys do, so another member's use makes the agent shared."""
    tenant = await make_tenant(db_session)
    other = await _member(db_session, tenant, "u7")
    if reach == "personal_default":
        await set_fields(
            db_session,
            scope=UserScopeRef(account_id=other.id),
            tenant_id=tenant.id,
            agent_name="unrouted",
        )
    elif reach == "thread_binding":
        await create_binding(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="c2",
            thread_id="t1",
            responder_ma_agent_id="agent_x",
            responder_name="unrouted",
            kind="handoff",
        )
    elif reach == "routine":
        await make_routine(
            db_session,
            tenant=tenant,
            created_by_user_id="u7",
            agent_id="agent_x",
            agent_name="unrouted",
            channel_id="c2",
        )
    else:
        await make_thread_session(
            db_session,
            tenant=tenant,
            account=other,
            thread_id="t1",
            ma_agent_id="agent_x",
            channel_id="c2",
        )
    me = await _member(db_session, tenant, "u5")

    async def shared(operation: OperationKind) -> bool:
        facts = await load_target_facts(
            db_session,
            operation,
            tenant_id=tenant.id,
            platform="discord",
            agent_names=("unrouted",),
            ma_agent_id="agent_x",
            default=DEFAULT,
            caller=ChannelAdminCaller(platform_user_id="u5"),
            is_daimon_managed=False,
            caller_account_id=me.id,
            caller_platform_user_id="u5",
        )
        return facts.is_reachable_in_tenant

    assert await shared("skill_repo_connect") and await shared("key_replace"), reach
    if reach in ("routine", "live_session"):
        assert not await shared("repo_bind"), "a repo bind keeps the cascade-only read"
