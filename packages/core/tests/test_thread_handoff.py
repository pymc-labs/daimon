"""Handing a thread to another agent: who may, decided under the tenant policy lock.

`authorize(HAND_OFF)` is pure and tested first. `hand_over_thread` is then
driven against real Postgres: refusals write nothing, and a policy edit racing
the switch on another connection is either read (committed first) or waits for
the switch's commit (arrives later), never neither.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentReach,
    AgentRef,
    Place,
    SessionFacts,
    Subject,
    Surface,
    authorize,
)
from daimon.core.channel_admins import ChannelAdminCaller
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_SEALED
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import lock_access_policy, set_access_policy
from daimon.core.stores.agent_creation_channels import record_creation_channel
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import TenantRow, ThreadAgentBindingRow
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.security_audit import append_event, list_events
from daimon.core.stores.thread_agent_bindings import create_binding, get_binding
from daimon.core.stores.thread_sessions import create_thread_session, get_thread_session_by_id
from daimon.core.thread_handoff import (
    HandoffCaller,
    HandoffDestination,
    RecordedSession,
    ThreadHandoffRefused,
    hand_over_thread,
    switch_thread_on_request,
)
from daimon.testing import ma_agent
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from daimon.testing.ma_models import ma_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
_DEFAULT = DeploymentDefault(agent_name="daimon")
_RESEARCH = HandoffDestination(
    ma_agent_id="agt_research", name="research-bot", agent=AgentRef.of("research-bot")
)
_THREAD = Place(channel_id="T1", parent_channel_id="C1")


def _hand_off(
    policy: TenantAccessPolicy,
    *,
    subject: Subject | None = None,
    agent: AgentRef | None = None,
    answers_here: bool = False,
    place: Place = _THREAD,
    reach: AgentReach | None = None,
) -> str | None:
    decision = authorize(
        policy,
        subject=subject or Subject(platform_user_id="U1"),
        action=Action.HAND_OFF,
        surface=Surface.HANDOFF,
        agent=agent or AgentRef.of("research-bot"),
        place=place,
        answers_here=answers_here,
        reach=reach,
    )
    return None if decision.allowed else decision.reason


# --- authorize(HAND_OFF) ------------------------------------------------------------


def test_a_member_may_hand_to_an_agent_scoped_to_the_channel() -> None:
    open_policy = TenantAccessPolicy()
    assert _hand_off(open_policy, answers_here=True) is None, "the agent the channel answers with"
    pinned_here = TenantAccessPolicy(agent_channel_pins={"research-bot": ("C1", "C2")})
    assert _hand_off(pinned_here) is None, "an agent pinned to this channel"
    isolated = TenantAccessPolicy(
        sealed_channel_ids=("C1",),
        isolated_channel_ids=("C1",),
        agent_channel_pins={"research-bot": ("C1",)},
    )
    assert _hand_off(isolated) is None, "one of an isolated channel's own agents"


_THEIRS = AgentReach(local_to_caller=True, held_by_caller=True)


def test_any_other_agent_needs_a_server_admin_or_a_channel_admin_who_could_bind_it() -> None:
    policy = TenantAccessPolicy()
    assert _hand_off(policy) == "admin_required"
    assert _hand_off(policy, subject=Subject(is_admin=True, platform_user_id="U1")) is None
    channel_admin = Subject(platform_user_id="U1", administered_channel_ids=frozenset({"C1"}))
    for reach in (_THEIRS, AgentReach(managed=True), AgentReach(tenant_wide=True)):
        assert _hand_off(policy, subject=channel_admin, reach=reach) is None, (
            f"a channel admin may bind {reach} as C1's default, so may hand to it"
        )
    for reach in (
        None,
        AgentReach(held_by_caller=True),
        AgentReach(local_to_caller=True),
        AgentReach(local_to_caller=True, held_by_caller=True, held_by_other_admin=True),
    ):
        assert _hand_off(policy, subject=channel_admin, reach=reach) == "admin_required", (
            f"{reach} is not the channel admin's to bring into C1"
        )
    other_channel = Subject(platform_user_id="U1", administered_channel_ids=frozenset({"C2"}))
    assert _hand_off(policy, subject=other_channel, reach=_THEIRS) == "admin_required"
    agent_key = Subject(is_admin=True, platform_user_id="U1", via_agent_key=True)
    assert _hand_off(policy, subject=agent_key) == "admin_required", "a key is never an admin"


def test_in_a_sealed_thread_a_channel_admin_cant_bring_in_an_outside_agent() -> None:
    sealed = TenantAccessPolicy(sealed_channel_ids=("C1",))
    channel_admin = Subject(platform_user_id="U1", administered_channel_ids=frozenset({"C1"}))
    assert _hand_off(sealed, subject=channel_admin) == "sealed"
    assert _hand_off(sealed, subject=Subject(is_admin=True, platform_user_id="U1")) is None
    assert _hand_off(sealed, answers_here=True) is None, "the channel's own agent still may"
    thread_sealed = TenantAccessPolicy(sealed_channel_ids=("C1:T1",))
    assert _hand_off(thread_sealed, subject=channel_admin) == "sealed", "a Slack thread seal"


def test_pins_isolation_protection_and_the_invoker_list_bind_admins_too() -> None:
    admin = Subject(is_admin=True, platform_user_id="U1")
    pinned_elsewhere = TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)})
    assert _hand_off(pinned_elsewhere, subject=admin) == "agent_pinned_elsewhere"
    assert _hand_off(pinned_elsewhere, answers_here=True) == "agent_pinned_elsewhere"
    isolated = TenantAccessPolicy(sealed_channel_ids=("C1",), isolated_channel_ids=("C1",))
    assert _hand_off(isolated, subject=admin, answers_here=True) == "channel_isolated"
    protected = TenantAccessPolicy(protected_channel_ids=("C1",))
    assert _hand_off(protected, subject=admin, answers_here=True) == "channel_protected"
    invokers = TenantAccessPolicy(invoker_user_ids=("U2",))
    assert _hand_off(invokers, answers_here=True) == "invoker_not_allowed"
    assert _hand_off(invokers, subject=admin) is None, "admins are never locked out"
    own_agent_out = TenantAccessPolicy(
        sealed_channel_ids=("C9",),
        isolated_channel_ids=("C9",),
        agent_channel_pins={"research-bot": ("C9",)},
    )
    assert _hand_off(own_agent_out, subject=admin) == "agent_pinned_elsewhere", (
        "an isolated channel's own agent never leaves it"
    )


def test_a_dm_scope_is_outside_every_pin() -> None:
    dm = Place.from_origin(parent_channel_id="D1", thread_id="dm:abc")
    pinned = TenantAccessPolicy(agent_channel_pins={"research-bot": ("C1",)})
    assert _hand_off(pinned, place=dm, answers_here=True) == "agent_pinned_elsewhere"
    assert _hand_off(TenantAccessPolicy(), place=dm, answers_here=True) is None


# --- hand_over_thread ---------------------------------------------------------------


async def _seed(session: AsyncSession, *, policy: TenantAccessPolicy | None = None) -> TenantRow:
    tenant = await make_tenant(session)
    # research-bot answers in C2, so it is reachable in the workspace.
    await set_fields(
        session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="C2"),
        tenant_id=tenant.id,
        agent_name="research-bot",
    )
    if policy is not None:
        await lock_access_policy(session, tenant_id=tenant.id)
        await set_access_policy(session, tenant_id=tenant.id, policy=policy)
    await session.commit()
    return tenant


def _caller(account_id: uuid.UUID | None = None, *, admin: bool = False) -> HandoffCaller:
    return HandoffCaller(
        account_id=account_id,
        channel_admin=ChannelAdminCaller(platform_user_id="U1", is_server_admin=admin),
    )


async def _switch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    caller: HandoffCaller | None = None,
    recorded: tuple[RecordedSession, ...] | None = None,
) -> ThreadAgentBindingRow:
    return await hand_over_thread(
        session,
        tenant_id=tenant_id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="T1",
        caller=caller or _caller(),
        destination=_RESEARCH,
        current_responder_ma_agent_id="agt_daimon",
        default=_DEFAULT,
        now=_NOW,
        recorded=recorded,
    )


async def _binding(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> ThreadAgentBindingRow | None:
    async with factory() as session:
        return await get_binding(
            session, tenant_id=tenant_id, platform="slack", parent_channel_id="C1", thread_id="T1"
        )


async def test_a_member_switch_to_an_agent_pinned_here_binds_the_thread(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = (
        await _seed(
            db_session,
            policy=TenantAccessPolicy(agent_channel_pins={"research-bot": ("C1", "C2")}),
        )
    ).id
    async with db_session_factory.begin() as session:
        await _switch(session, tenant_id)
    binding = await _binding(db_session_factory, tenant_id)
    assert binding is not None and binding.kind == "handoff"
    assert binding.responder_ma_agent_id == "agt_research"


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)}), "pinned_elsewhere"),
        (
            TenantAccessPolicy(sealed_channel_ids=("C1",), isolated_channel_ids=("C1",)),
            "channel_isolated",
        ),
        (TenantAccessPolicy(protected_channel_ids=("C1",)), "channel_protected"),
        (TenantAccessPolicy(sealed_channel_ids=("C1",)), "sealed"),
    ],
    ids=["pinned-elsewhere", "isolated", "protected", "sealed"],
)
async def test_a_refused_switch_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
    reason: str,
) -> None:
    """The sealed case is a channel admin of C1 bringing in research-bot."""
    tenant_id = (await _seed(db_session, policy=policy)).id
    async with db_session_factory.begin() as session:
        await set_channel_admins(
            session,
            tenant_id=tenant_id,
            platform="slack",
            channel_id="C1",
            role_ids=(),
            user_ids=("U1",),
            actor_account_id=None,
        )
    with pytest.raises(ThreadHandoffRefused) as refused:
        async with db_session_factory.begin() as session:
            await _switch(session, tenant_id)
    assert refused.value.refusal.reason == reason
    assert await _binding(db_session_factory, tenant_id) is None


async def test_a_setup_thread_is_never_switched(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = (await _seed(db_session)).id
    async with db_session_factory.begin() as session:
        await create_binding(
            session,
            tenant_id=tenant_id,
            platform="slack",
            parent_channel_id="C1",
            thread_id="T1",
            responder_ma_agent_id="agt_daimon",
            responder_name="daimon",
        )
    with pytest.raises(ThreadHandoffRefused) as refused:
        async with db_session_factory.begin() as session:
            await _switch(session, tenant_id, _caller(admin=True))
    assert refused.value.refusal.reason == "setup_thread"


# --- Races on separate connections ----------------------------------------------------


@pytest_asyncio.fixture
async def race_engine(db_engine: AsyncEngine, db_schema: str) -> AsyncIterator[AsyncEngine]:
    engine = build_test_engine(
        db_engine.url.render_as_string(hide_password=False), db_schema, poolclass=NullPool
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _pid(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _until_lock_wait(engine: AsyncEngine, pid: int) -> None:
    async with engine.connect() as probe:
        for _ in range(200):
            waiting = (
                await probe.execute(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
            ).scalar_one_or_none()
            if waiting == "Lock":
                return
            await probe.rollback()
            await asyncio.sleep(0.025)
    raise AssertionError(f"backend {pid} never waited on a lock")


_PIN_ELSEWHERE = TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)})


async def test_a_pin_committed_before_the_decision_refuses_the_switch(
    db_session: AsyncSession, race_engine: AsyncEngine
) -> None:
    """The edit holds the policy lock first: the switch waits, reads it, refuses."""
    tenant_id = (await _seed(db_session)).id
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    switcher_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def switch() -> None:
        async with factory.begin() as session:
            switcher_pid.set_result(await _pid(session))
            await _switch(session, tenant_id, _caller(admin=True))

    async with factory() as editor, editor.begin():
        await lock_access_policy(editor, tenant_id=tenant_id)
        await set_access_policy(editor, tenant_id=tenant_id, policy=_PIN_ELSEWHERE)
        switching = asyncio.create_task(switch())
        await _until_lock_wait(race_engine, await switcher_pid)
    with pytest.raises(ThreadHandoffRefused) as refused:
        await asyncio.wait_for(switching, 10)
    assert refused.value.refusal.reason == "pinned_elsewhere"
    assert await _binding(factory, tenant_id) is None, "nothing was switched"


async def test_a_pin_arriving_after_the_decision_waits_for_the_switch(
    db_session: AsyncSession, race_engine: AsyncEngine
) -> None:
    """The switch decided first: the edit waits for its commit, then applies."""
    tenant_id = (await _seed(db_session)).id
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    decided = asyncio.Event()
    release = asyncio.Event()
    editor_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    bound_when_edit_locked: list[bool] = []

    async def switch() -> None:
        async with factory.begin() as session:
            await _switch(session, tenant_id, _caller(admin=True))
            decided.set()
            # The tool writes its continuation here, still under the lock.
            await release.wait()

    async def edit() -> None:
        async with factory.begin() as session:
            editor_pid.set_result(await _pid(session))
            await lock_access_policy(session, tenant_id=tenant_id)
            bound = await get_binding(
                session,
                tenant_id=tenant_id,
                platform="slack",
                parent_channel_id="C1",
                thread_id="T1",
            )
            bound_when_edit_locked.append(bound is not None)
            await set_access_policy(session, tenant_id=tenant_id, policy=_PIN_ELSEWHERE)

    switching = asyncio.create_task(switch())
    editing: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(decided.wait(), 10)
        editing = asyncio.create_task(edit())
        await _until_lock_wait(race_engine, await editor_pid)
        assert not editing.done(), "the edit waits while the switch is pending"
    finally:
        release.set()
        await asyncio.wait_for(switching, 10)
        if editing is not None:
            await asyncio.wait_for(editing, 10)
    assert bound_when_edit_locked == [True], "the switch committed before the edit took the lock"


async def test_the_switch_neither_blocks_nor_deadlocks_with_audit_writes_or_an_account_purge(
    db_session: AsyncSession, race_engine: AsyncEngine
) -> None:
    """An account purge holds the account FOR UPDATE first; the switch waits there
    holding only the policy lock, which the purge never takes, and goes on once the
    purge commits. An audit write (KEY SHARE on tenant and account) runs while the
    switch holds every lock."""
    tenant = await _seed(db_session)
    tenant_id = tenant.id
    account_id = (await make_account(db_session, tenant=tenant)).id
    await db_session.commit()
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    switcher_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    decided = asyncio.Event()
    release = asyncio.Event()

    async def switch() -> None:
        async with factory.begin() as session:
            switcher_pid.set_result(await _pid(session))
            await _switch(session, tenant_id, _caller(account_id, admin=True))
            decided.set()
            await release.wait()

    switching: asyncio.Task[None] | None = None
    try:
        async with factory() as purge, purge.begin():
            await purge.execute(
                text("SELECT id FROM accounts WHERE id = :id FOR UPDATE"), {"id": account_id}
            )
            switching = asyncio.create_task(switch())
            await _until_lock_wait(race_engine, await switcher_pid)
        await asyncio.wait_for(decided.wait(), 10)
        async with factory.begin() as audit:
            written = await asyncio.wait_for(
                append_event(
                    audit,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    agent_id=None,
                    platform="slack",
                    platform_user_id="U1",
                    tool_name="hand_off_task",
                    operation=None,
                    outcome="allowed",
                    reason="race",
                ),
                5,
            )
        assert written is not None, "an audit write is not blocked by a pending switch"
    finally:
        release.set()
        if switching is not None:
            await asyncio.wait_for(switching, 10)
    assert await _binding(factory, tenant_id) is not None


# --- Recorded seals, unnamed agents, and an account purge -----------------------------


def _channel_admin_of_c1() -> HandoffCaller:
    return HandoffCaller(
        account_id=None,
        channel_admin=ChannelAdminCaller(platform_user_id="U1"),
    )


async def _grant_c1(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    await _grant(factory, tenant_id, "C1", "U1")


async def _grant(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, channel_id: str, *users: str
) -> None:
    async with factory.begin() as session:
        await set_channel_admins(
            session,
            tenant_id=tenant_id,
            platform="slack",
            channel_id=channel_id,
            role_ids=(),
            user_ids=users,
            actor_account_id=None,
        )


async def _research_made_for_c2(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """A channel admin of C2 made research-bot there; it answers in C2 alone (`_seed`)."""
    async with factory.begin() as session:
        await record_creation_channel(
            session,
            tenant_id=tenant_id,
            ma_agent_id="agt_research",
            platform="slack",
            channel_id="C2",
        )


@pytest.mark.parametrize(
    ("c2_admins", "allowed"),
    [((), False), (("U1",), True), (("U1", "U2"), False)],
    ids=["another-channels-agent", "their-own-agent", "another-admin-would-lose-it"],
)
async def test_a_channel_admin_hands_a_thread_only_to_an_agent_they_could_bind_here(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    c2_admins: tuple[str, ...],
    allowed: bool,
) -> None:
    """research-bot was made for C2 and answers there. U1 administers C1. Unless U1
    administers C2 too, it is C2's own agent, and answering in C1 would lend C1 its
    keys and memory. If U2 administers only C2, the binding would take research-bot
    out of U2's channels and cost U2 their rights over it."""
    tenant_id = (await _seed(db_session)).id
    await _grant_c1(db_session_factory, tenant_id)
    if c2_admins:
        await _grant(db_session_factory, tenant_id, "C2", *c2_admins)
    await _research_made_for_c2(db_session_factory, tenant_id)

    async def switch() -> None:
        async with db_session_factory.begin() as session:
            await _switch(session, tenant_id, _channel_admin_of_c1(), recorded=())

    if allowed:
        await switch()
        assert await _binding(db_session_factory, tenant_id) is not None, "U1's own agent"
    else:
        with pytest.raises(ThreadHandoffRefused) as refused:
            await switch()
        assert refused.value.refusal.reason == "admin_required", "a server admin's call"
        assert await _binding(db_session_factory, tenant_id) is None, "nothing was switched"


async def test_a_server_admin_hands_a_thread_to_another_channels_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = (await _seed(db_session)).id
    await _grant(db_session_factory, tenant_id, "C2", "U2")
    await _research_made_for_c2(db_session_factory, tenant_id)
    async with db_session_factory.begin() as session:
        await _switch(session, tenant_id, _caller(admin=True))
    assert await _binding(db_session_factory, tenant_id) is not None, "a server admin's call"


@pytest.mark.parametrize(
    ("recorded", "refused"),
    [
        ((), False),
        ((RecordedSession(account_id=None, facts=SessionFacts(seal_ids=frozenset({"C1"})))), True),
        (None, True),
    ],
    ids=["no-sealed-session", "session-sealed-before-an-unseal", "seals-not-read"],
)
async def test_a_channel_admin_cant_hand_a_once_sealed_session_to_an_outside_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    recorded: Any,
    refused: bool,
) -> None:
    """C1 is not sealed now, but a live session in the thread was sealed before an
    admin lifted the seal: its content is still sealed content."""
    tenant_id = (await _seed(db_session)).id
    await _grant_c1(db_session_factory, tenant_id)
    # research-bot is U1's own, so only the seal can refuse it.
    await _grant(db_session_factory, tenant_id, "C2", "U1")
    await _research_made_for_c2(db_session_factory, tenant_id)
    sessions = (recorded,) if isinstance(recorded, RecordedSession) else recorded

    async def switch() -> None:
        async with db_session_factory.begin() as session:
            await hand_over_thread(
                session,
                tenant_id=tenant_id,
                platform="slack",
                parent_channel_id="C1",
                thread_id="T1",
                caller=_channel_admin_of_c1(),
                destination=_RESEARCH,
                current_responder_ma_agent_id="agt_daimon",
                default=_DEFAULT,
                now=_NOW,
                recorded=sessions,
            )

    if refused:
        with pytest.raises(ThreadHandoffRefused) as error:
            await switch()
        assert error.value.refusal.reason == "sealed"
        assert await _binding(db_session_factory, tenant_id) is None
    else:
        await switch()
        assert await _binding(db_session_factory, tenant_id) is not None


async def test_the_button_refuses_an_agent_with_no_configuration_name(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Same rule as `hand_off_task`: the cascade and the binding name agents by it."""
    tenant_id = (await _seed(db_session)).id
    router = MARouter()
    router.add_agent(
        ma_agent(
            id="agt_research",
            name="research-bot",
            tenant_id=tenant_id,
            metadata={MA_METADATA_KEY_NAME: ""},
        )
    )
    outcome = await switch_thread_on_request(
        build_fake_anthropic(router.dispatch),
        db_session_factory,
        tenant_id=tenant_id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="T1",
        ma_agent_id="agt_research",
        caller=ChannelAdminCaller(platform_user_id="U1", is_server_admin=True),
        default=_DEFAULT,
        channel="#c1",
        now=_NOW,
    )
    assert not outcome.switched
    assert "no configuration name" in outcome.text
    assert await _binding(db_session_factory, tenant_id) is None


async def test_a_purge_of_the_binding_creators_account_and_a_switch_never_deadlock(
    db_session: AsyncSession, race_engine: AsyncEngine
) -> None:
    """The purge takes the account FOR UPDATE, then (deleting the account) nulls
    `creator_account_id` on the bindings it created. The switch takes the account
    FOR KEY SHARE before the binding row, so it waits there without holding the
    row the purge needs. Taking the binding first deadlocks the two."""
    tenant = await _seed(db_session)
    account_id = (await make_account(db_session, tenant=tenant)).id
    await db_session.commit()
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    async with factory.begin() as session:
        await create_binding(
            session,
            tenant_id=tenant.id,
            platform="slack",
            parent_channel_id="C1",
            thread_id="T1",
            responder_ma_agent_id="agt_daimon",
            responder_name="daimon",
            creator_account_id=account_id,
            kind="handoff",
        )
    switcher_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def switch() -> None:
        async with factory.begin() as session:
            switcher_pid.set_result(await _pid(session))
            await _switch(session, tenant.id, _caller(account_id, admin=True))

    switching: asyncio.Task[None] | None = None
    try:
        async with factory() as purge, purge.begin():
            await purge.execute(
                text("SELECT id FROM accounts WHERE id = :id FOR UPDATE"), {"id": account_id}
            )
            switching = asyncio.create_task(switch())
            await _until_lock_wait(race_engine, await switcher_pid)
            # What deleting the account does to the bindings it created.
            await asyncio.wait_for(
                purge.execute(
                    text(
                        "UPDATE thread_agent_bindings SET creator_account_id = NULL "
                        "WHERE creator_account_id = :id"
                    ),
                    {"id": account_id},
                ),
                5,
            )
    finally:
        if switching is not None:
            await asyncio.wait_for(switching, 10)
    binding = await _binding(factory, tenant.id)
    assert binding is not None and binding.responder_ma_agent_id == "agt_research"
    assert binding.creator_account_id is None, "the purge committed before the switch wrote"


@pytest.mark.parametrize("seal_ids", [None, frozenset(), frozenset({"C1"})])
async def test_channel_admin_switch_refuses_unread_session_without_persisting_sentinel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    seal_ids: frozenset[str] | None,
) -> None:
    tenant = await _seed(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    await _grant_c1(db_session_factory, tenant.id)
    async with db_session_factory.begin() as session:
        row = await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C1",
            thread_id="T1",
            account_id=account.id,
            ma_session_id="sess_unread",
            ma_agent_id="agt_daimon",
            seal_ids=seal_ids,
        )
    router = MARouter()
    router.add_agent(ma_agent(id="agt_research", name="research-bot", tenant_id=tenant.id))
    router.add(
        "GET",
        r"/v1/sessions/sess_unread",
        lambda _r, _m: httpx.Response(
            500,
            json={"type": "error", "error": {"type": "api_error", "message": "unavailable"}},
        ),
    )
    client = build_fake_anthropic(router.dispatch)
    client.max_retries = 0
    outcome = await switch_thread_on_request(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="T1",
        ma_agent_id="agt_research",
        caller=ChannelAdminCaller(platform_user_id="U1"),
        default=_DEFAULT,
        channel="#c1",
        now=_NOW,
    )
    assert not outcome.switched
    assert "sealed" in outcome.text
    assert await _binding(db_session_factory, tenant.id) is None
    async with db_session_factory() as session:
        unchanged = await get_thread_session_by_id(session, id=row.id)
    assert unchanged is not None
    assert unchanged.seal_ids == (None if seal_ids is None else tuple(sorted(seal_ids)))


async def test_channel_admin_switch_refuses_legacy_seal_without_a_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A legacy bare "true" seal with no channel reads as a seal nobody is
    # inside. It is not stored (JSONB rejects the sentinel) and still refuses.
    tenant = await _seed(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    await _grant_c1(db_session_factory, tenant.id)
    async with db_session_factory.begin() as session:
        row = await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="slack",
            channel_id="C1",
            thread_id="T1",
            account_id=account.id,
            ma_session_id="sess_legacy",
            ma_agent_id="agt_daimon",
            seal_ids=None,
        )
    router = MARouter()
    router.add_agent(ma_agent(id="agt_research", name="research-bot", tenant_id=tenant.id))
    router.add_session(ma_session(id="sess_legacy", metadata={MA_METADATA_KEY_SEALED: "true"}))
    outcome = await switch_thread_on_request(
        build_fake_anthropic(router.dispatch),
        db_session_factory,
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="T1",
        ma_agent_id="agt_research",
        caller=ChannelAdminCaller(platform_user_id="U1"),
        default=_DEFAULT,
        channel="#c1",
        now=_NOW,
    )
    assert not outcome.switched
    assert "sealed" in outcome.text
    async with db_session_factory() as session:
        unchanged = await get_thread_session_by_id(session, id=row.id)
    assert unchanged is not None and unchanged.seal_ids is None


@pytest.mark.parametrize(
    ("server_admin", "row"),
    [
        (True, ("panel:handoff", "allowed", "completed")),
        (False, ("panel:handoff", "denied", "authz:admin_required")),
    ],
    ids=["server-admin-allowed", "channel-admin-refused"],
)
async def test_a_hand_over_click_by_an_admin_is_audited(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    server_admin: bool,
    row: tuple[str, str, str],
) -> None:
    """As `hand_off_task`'s trail: a click resting on admin standing, or refused by
    `authorize`, writes a `panel:handoff` row. research-bot is C2's own agent."""
    tenant_id = (await _seed(db_session)).id
    await _grant_c1(db_session_factory, tenant_id)
    router = MARouter()
    router.add_agent(ma_agent(id="agt_research", name="research-bot", tenant_id=tenant_id))
    outcome = await switch_thread_on_request(
        build_fake_anthropic(router.dispatch),
        db_session_factory,
        tenant_id=tenant_id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="T1",
        ma_agent_id="agt_research",
        caller=ChannelAdminCaller(platform_user_id="U1", is_server_admin=server_admin),
        default=_DEFAULT,
        channel="#c1",
        now=_NOW,
    )
    assert outcome.switched is server_admin, "only the server admin may bring it into C1"
    async with db_session_factory() as session:
        events = await list_events(session, tenant_id=tenant_id)
    assert [(e.tool_name, e.outcome, e.reason) for e in events] == [row], (
        "one panel row per admin-tier click"
    )
