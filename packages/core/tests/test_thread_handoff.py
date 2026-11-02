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
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentReach,
    AgentRef,
    Place,
    Subject,
    Surface,
    authorize,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.native_admins import AdminCaller
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import lock_access_policy, set_access_policy
from daimon.core.stores.domain import TenantRow, ThreadAgentBindingRow
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.security_audit import append_event, list_events
from daimon.core.stores.thread_agent_bindings import create_binding, get_binding
from daimon.core.thread_handoff import (
    HandoffCaller,
    HandoffDestination,
    RecordedSession,
    ThreadHandoffRefused,
    hand_over_thread,
    switch_thread_on_request,
)
from daimon.core.turn_hold import TurnHold
from daimon.testing.db import build_test_engine
from daimon.testing.factories import make_account, make_tenant, make_thread_session
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic
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


_THEIRS = AgentReach()


def test_pins_isolation_protection_and_the_invoker_list_bind_admins_too() -> None:
    admin = Subject(is_admin=True, platform_user_id="U1")
    pinned_elsewhere = TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)})
    assert _hand_off(pinned_elsewhere, subject=admin) == "runs_elsewhere"
    assert _hand_off(pinned_elsewhere, answers_here=True) == "runs_elsewhere"
    isolated = TenantAccessPolicy(sealed_channel_ids=("C1",), isolated_channel_ids=("C1",))
    assert _hand_off(isolated, subject=admin, answers_here=True) == "own_agents_only"
    protected = TenantAccessPolicy(protected_channel_ids=("C1",))
    assert _hand_off(protected, subject=admin, answers_here=True) == "writers_none"
    invokers = TenantAccessPolicy(invoker_user_ids=("U2",))
    assert _hand_off(invokers, answers_here=True) == "invoker_not_allowed"
    assert _hand_off(invokers, subject=admin) is None, "admins are never locked out"
    own_agent_out = TenantAccessPolicy(
        sealed_channel_ids=("C9",),
        isolated_channel_ids=("C9",),
        agent_channel_pins={"research-bot": ("C9",)},
    )
    assert _hand_off(own_agent_out, subject=admin) == "runs_elsewhere", (
        "an isolated channel's own agent never leaves it"
    )


def test_a_dm_scope_is_outside_every_pin() -> None:
    dm = Place.from_origin(parent_channel_id="D1", thread_id="dm:abc")
    pinned = TenantAccessPolicy(agent_channel_pins={"research-bot": ("C1",)})
    assert _hand_off(pinned, place=dm, answers_here=True) == "runs_elsewhere"
    assert _hand_off(TenantAccessPolicy(), place=dm, answers_here=True) is None


# --- hand_over_thread ---------------------------------------------------------------


async def _seed(session: AsyncSession, *, policy: TenantAccessPolicy | None = None) -> TenantRow:
    tenant = await make_tenant(session, platform="slack")
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


async def _binding(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> ThreadAgentBindingRow | None:
    async with factory() as session:
        return await get_binding(
            session, tenant_id=tenant_id, platform="slack", parent_channel_id="C1", thread_id="T1"
        )


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


# --- Recorded seals, unnamed agents, and an account purge -----------------------------


def _caller(account_id: uuid.UUID | None = None, *, admin: bool = False) -> HandoffCaller:
    return HandoffCaller(
        account_id=account_id,
        admin=AdminCaller(platform_user_id="U1", is_server_admin=admin),
        human_invocation=True,
    )


async def _switch(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    caller: HandoffCaller | None = None,
    recorded: tuple[RecordedSession, ...] | None = None,
    destination: HandoffDestination = _RESEARCH,
) -> ThreadAgentBindingRow:
    return await hand_over_thread(
        session,
        tenant_id=tenant_id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="T1",
        caller=caller or _caller(),
        destination=destination,
        current_responder_ma_agent_id="agt_daimon",
        default=_DEFAULT,
        now=_NOW,
        recorded=recorded,
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
        (TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)}), "runs_elsewhere"),
        (
            TenantAccessPolicy(sealed_channel_ids=("C1",), isolated_channel_ids=("C1",)),
            "own_agents_only",
        ),
        (TenantAccessPolicy(protected_channel_ids=("C1",)), "writers_none"),
        (TenantAccessPolicy(sealed_channel_ids=("C1",)), "admin_required"),
    ],
    ids=["pinned-elsewhere", "isolated", "protected", "sealed"],
)
async def test_a_refused_switch_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
    reason: str,
) -> None:
    """A refused switch leaves no binding."""
    tenant_id = (await _seed(db_session, policy=policy)).id
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
    assert refused.value.refusal.reason == "runs_elsewhere"
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
        caller=AdminCaller(platform_user_id="U1", is_server_admin=True),
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


@pytest.mark.parametrize(
    ("server_admin", "row"),
    [
        (True, ("panel:handoff", "allowed", "completed")),
        (False, ("panel:handoff", "denied", "authz:admin_required")),
    ],
    ids=["server-admin-allowed", "member-refused"],
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
        caller=AdminCaller(platform_user_id="U1", is_server_admin=server_admin),
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


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize("agent_id", ["sh_agent_test", "agt_old_button"])
async def test_selfhosted_handover_click_refuses_before_anthropic(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: str,
    agent_id: str,
) -> None:
    from typing import cast

    from anthropic import AsyncAnthropic
    from daimon.core.channel_backend import channel_ref, set_channel_backend
    from mux.contracts.config import BackendConfig

    class NoAnthropic:
        def __getattribute__(self, name: str) -> object:
            raise AssertionError(f"handover touched Anthropic.{name}")

    tenant = await make_tenant(db_session, platform=platform)
    await set_channel_backend(
        db_session,
        channel_ref(tenant.id, platform, "channel"),
        BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna"),
    )
    await db_session.commit()
    result = await switch_thread_on_request(
        cast(AsyncAnthropic, NoAnthropic()),
        db_session_factory,
        tenant_id=tenant.id,
        platform=platform,
        parent_channel_id="channel",
        thread_id="thread",
        ma_agent_id=agent_id,
        caller=AdminCaller(platform_user_id="user", is_server_admin=True),
        default=_DEFAULT,
        channel="this channel",
        now=_NOW,
    )
    assert not result.switched
    assert "self-hosted" in result.text and "Nothing was changed" in result.text


@pytest.mark.parametrize("seed", [None, "yes", " research-bot ", "x" * 600])
def test_button_seed_is_sanitized_and_bounded(seed: str | None) -> None:
    from daimon.core.thread_handoff import HANDOVER_FALLBACK_SEED, handover_seed

    assert handover_seed(seed, agent_name="research-bot") == (
        "x" * 500 if seed and len(seed) > 500 else HANDOVER_FALLBACK_SEED
    )


async def test_racing_button_clickers_switch_once_and_queue_no_continuation(
    db_session: AsyncSession, race_engine: AsyncEngine
) -> None:
    from daimon.core.stores.task_continuations import list_pending_continuations

    tenant = await _seed(db_session)
    factory = async_sessionmaker(race_engine, expire_on_commit=False)
    router = MARouter()
    router.add_agent(ma_agent(id="agt_research", name="research-bot", tenant_id=tenant.id))
    client = build_fake_anthropic(router.dispatch)

    async def click(user: str) -> bool:
        outcome = await switch_thread_on_request(
            client,
            factory,
            tenant_id=tenant.id,
            platform="slack",
            parent_channel_id="C1",
            thread_id="T1",
            ma_agent_id="agt_research",
            caller=AdminCaller(platform_user_id=user, is_server_admin=True),
            default=_DEFAULT,
            channel="#c1",
            now=_NOW,
        )
        return outcome.switched

    assert sum(await asyncio.gather(click("U1"), click("U2"))) == 1
    async with factory() as session:
        assert (
            await list_pending_continuations(
                session, tenant_id=tenant.id, platform="slack", thread_id="T1"
            )
            == []
        )


async def test_handoff_role_gate_fetches_target_invoker_membership_before_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.access_policy import AgentRule
    from daimon.core.membership import CachedMembershipResolver, Membership

    tenant = await make_tenant(db_session, platform="discord", workspace_id="111")
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            agent_rules={"research-bot": AgentRule(invoker_role_ids=("123",))}
        ),
    )
    await db_session.commit()
    facts = [Membership(is_member=True, is_admin=True)]

    async def fetch(*_args: object) -> Membership:
        return facts[0]

    resolver = CachedMembershipResolver(fetch)
    async with db_session_factory() as session:
        with pytest.raises(ThreadHandoffRefused) as exc:
            await hand_over_thread(
                session,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="C1",
                thread_id="T1",
                caller=_caller(admin=True),
                destination=_RESEARCH,
                current_responder_ma_agent_id="agt_daimon",
                default=_DEFAULT,
                now=_NOW,
                membership_resolver=resolver,
            )
        assert exc.value.refusal.reason == "invoker_not_allowed"
        assert (
            await get_binding(
                session,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="C1",
                thread_id="T1",
            )
            is None
        )


# --- A person hands this thread to an agent they own (TARGET-V2 J1/J2) ------------------

_COPY = HandoffDestination(ma_agent_id="agt_copy", name="Daimon-2", agent=AgentRef.of("Daimon-2"))


def test_a_person_may_hand_this_thread_to_an_agent_they_own() -> None:
    open_policy = TenantAccessPolicy()
    mine = AgentReach(owned=True)
    assert _hand_off(open_policy, reach=mine) is None
    assert _hand_off(open_policy, reach=_THEIRS) == "admin_required"
    for subject in (
        Subject(platform_user_id="U1", via_agent_key=True),
        Subject(platform_user_id="U1", client=True),
        Subject(),
    ):
        assert _hand_off(open_policy, subject=subject, reach=mine) is not None, subject
    held = authorize(
        open_policy,
        subject=Subject(platform_user_id="U1"),
        action=Action.HAND_OFF,
        surface=Surface.HANDOFF,
        agent=AgentRef.of("research-bot"),
        place=_THREAD,
        reach=mine,
        hold=TurnHold(platform="slack", channel_id="C1", client=False),
    )
    assert not held.allowed
    # Owning an agent never lifts a channel's own rules.
    pinned_elsewhere = TenantAccessPolicy(agent_channel_pins={"research-bot": ("C9",)})
    assert _hand_off(pinned_elsewhere, reach=mine) == "runs_elsewhere"


async def _owned_copy(session: AsyncSession, tenant: TenantRow, owner: uuid.UUID) -> None:
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_owners

    await agent_owners.set_owner(
        session,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_copy"),
        owner_account_id=owner,
        source="created",
    )


async def test_a_member_hands_this_thread_to_their_own_unrouted_copy(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    await db_session.commit()
    async with db_session_factory.begin() as session:
        await _switch(
            session,
            tenant.id,
            caller=HandoffCaller(
                account_id=member.id,
                admin=AdminCaller(platform_user_id="U1", is_server_admin=False),
                may_own=True,
            ),
            destination=_COPY,
        )
    binding = await _binding(db_session_factory, tenant.id)
    assert binding is not None and binding.responder_ma_agent_id == "agt_copy"


@pytest.mark.parametrize("who", ["someone_elses", "unowned", "not_opted_in", "agent_key"])
async def test_an_unrouted_agent_the_caller_does_not_own_stays_admin_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], who: str
) -> None:
    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    if who != "unowned":
        await _owned_copy(db_session, tenant, other.id if who == "someone_elses" else member.id)
    await db_session.commit()
    caller = HandoffCaller(
        account_id=member.id,
        admin=AdminCaller(platform_user_id="U1", is_server_admin=False),
        # The owner path is only for a caller that checked the credential is a person's own.
        may_own=who != "not_opted_in",
        via_agent_key=who == "agent_key",
    )
    with pytest.raises(ThreadHandoffRefused):
        async with db_session_factory.begin() as session:
            await _switch(session, tenant.id, caller=caller, destination=_COPY)
    assert await _binding(db_session_factory, tenant.id) is None, "nothing is written"


def _owner_caller(account_id: uuid.UUID) -> HandoffCaller:
    return HandoffCaller(
        account_id=account_id,
        admin=AdminCaller(platform_user_id="U1", is_server_admin=False),
        may_own=True,
    )


async def test_an_owner_binding_is_marked_and_answers_only_its_owner(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.errors import UserFacingError
    from daimon.core.scope import ScopeContext
    from daimon.core.stores.scoped_config_read import resolve

    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    await db_session.commit()
    async with db_session_factory.begin() as session:
        await _switch(session, tenant.id, caller=_owner_caller(member.id), destination=_COPY)
    binding = await _binding(db_session_factory, tenant.id)
    assert binding is not None and binding.kind == "owner"
    assert binding.creator_account_id == member.id

    def here(account_id: uuid.UUID) -> ScopeContext:
        return ScopeContext(
            tenant_id=tenant.id,
            channel_id="C1",
            account_id=account_id,
            platform="slack",
            thread_id="T1",
        )

    async with db_session_factory() as session:
        mine = await resolve(session, context=here(member.id), default=_DEFAULT)
        assert mine.responder_ma_agent_id == "agt_copy"
        # Someone else speaking in the thread is never run on the private copy.
        with pytest.raises(UserFacingError, match="private copy"):
            await resolve(session, context=here(other.id), default=_DEFAULT)


@pytest.mark.parametrize("spoke", ["live_session", "closed_session", "recent_turn"])
async def test_a_thread_other_people_are_in_never_moves_to_a_private_copy(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], spoke: str
) -> None:
    from daimon.core.stores import thread_sessions
    from daimon.core.stores.domain import Role
    from daimon.core.stores.turn_origins import create_origin
    from daimon.core.thread_handoff import render_handoff_refused

    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    if spoke == "recent_turn":
        # Their turn has no session of its own yet (it is still starting).
        await create_origin(
            db_session,
            tenant_id=tenant.id,
            account_id=other.id,
            platform="slack",
            parent_channel_id="C1",
            thread_id="T1",
            responder_ma_agent_id="agt_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=_NOW + timedelta(hours=1),
            now=_NOW,
            sender_foreign=False,
            shared_channel=False,
        )
    else:
        theirs = await make_thread_session(
            db_session,
            tenant=tenant,
            account=other,
            platform="slack",
            thread_id="T1",
            channel_id="C1",
        )
        if spoke == "closed_session":
            await thread_sessions.mark_dead(db_session, id=theirs.id)
    await db_session.commit()
    with pytest.raises(ThreadHandoffRefused) as refused:
        async with db_session_factory.begin() as session:
            await _switch(session, tenant.id, caller=_owner_caller(member.id), destination=_COPY)
    assert refused.value.refusal.reason == "shared_thread"
    assert render_handoff_refused(refused.value.refusal, channel="C1").startswith(
        "Other people are in this thread, so I can't move it to your private copy. "
        "Start a new thread with me and ask again."
    )
    assert await _binding(db_session_factory, tenant.id) is None, "nothing is written"


async def test_a_turn_long_past_with_no_session_never_blocks_the_owner(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.stores.domain import Role
    from daimon.core.stores.turn_origins import create_origin

    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=other.id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="T1",
        responder_ma_agent_id="agt_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        expires_at=_NOW - timedelta(minutes=1),
        now=_NOW - timedelta(hours=2),
        sender_foreign=False,
        shared_channel=False,
    )
    await db_session.commit()
    async with db_session_factory.begin() as session:
        await _switch(session, tenant.id, caller=_owner_caller(member.id), destination=_COPY)
    binding = await _binding(db_session_factory, tenant.id)
    assert binding is not None and binding.kind == "owner"


async def test_the_owners_own_sessions_and_turns_never_count_as_someone_else(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.stores import thread_sessions
    from daimon.core.stores.domain import Role
    from daimon.core.stores.turn_origins import create_origin

    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    # Only the owner has ever been here: an earlier session of theirs (closed), the
    # live one, and their own turn now.
    closed = await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    await thread_sessions.mark_dead(db_session, id=closed.id)
    await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=member.id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="T1",
        responder_ma_agent_id="agt_daimon",
        responder_name="Daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        expires_at=_NOW + timedelta(hours=1),
        now=_NOW,
        sender_foreign=False,
        shared_channel=False,
    )
    await db_session.commit()
    async with db_session_factory.begin() as session:
        await _switch(session, tenant.id, caller=_owner_caller(member.id), destination=_COPY)
    binding = await _binding(db_session_factory, tenant.id)
    assert binding is not None and binding.kind == "owner"


async def test_a_session_with_no_account_counts_as_someone_else(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Fail closed. Every session written today carries an account (a shared thread's,
    its shared owner's), so an account-less row is legacy and nobody can say it was
    the caller's."""
    from daimon.core._models import ThreadSession
    from sqlalchemy import update

    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    legacy = await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    await db_session.execute(
        update(ThreadSession).where(ThreadSession.id == legacy.id).values(account_id=None)
    )
    await db_session.commit()
    with pytest.raises(ThreadHandoffRefused) as refused:
        async with db_session_factory.begin() as session:
            await _switch(session, tenant.id, caller=_owner_caller(member.id), destination=_COPY)
    assert refused.value.refusal.reason == "shared_thread"
    assert await _binding(db_session_factory, tenant.id) is None


@pytest.mark.parametrize(
    ("policy", "reason"),
    [
        (TenantAccessPolicy(agent_channel_pins={"Daimon-2": ("C9",)}), "runs_elsewhere"),
        (
            TenantAccessPolicy(sealed_channel_ids=("C1",), isolated_channel_ids=("C1",)),
            "own_agents_only",
        ),
    ],
    ids=["pinned-elsewhere", "isolated"],
)
async def test_owning_a_copy_never_lifts_the_channels_rules(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy,
    reason: str,
) -> None:
    tenant = await _seed(db_session, policy=policy)
    member = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    await db_session.commit()
    with pytest.raises(ThreadHandoffRefused) as refused:
        async with db_session_factory.begin() as session:
            await _switch(session, tenant.id, caller=_owner_caller(member.id), destination=_COPY)
    assert refused.value.refusal.reason == reason
    assert await _binding(db_session_factory, tenant.id) is None


@pytest.mark.parametrize("change", ["reassigned", "cleared"])
async def test_an_owner_thread_stops_routing_when_its_copy_changes_owner(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], change: str
) -> None:
    from daimon.core.errors import UserFacingError
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.scope import ScopeContext
    from daimon.core.stores import agent_owners
    from daimon.core.stores.scoped_config_read import resolve

    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    await db_session.commit()
    async with db_session_factory.begin() as session:
        await _switch(session, tenant.id, caller=_owner_caller(member.id), destination=_COPY)
    copy_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agt_copy")
    async with db_session_factory.begin() as session:
        await agent_owners.clear_owner(session, tenant_id=tenant.id, agent_id=copy_id)
        if change == "reassigned":
            await agent_owners.set_owner(
                session,
                tenant_id=tenant.id,
                agent_id=copy_id,
                owner_account_id=other.id,
                source="admin_assigned",
            )
    here = ScopeContext(
        tenant_id=tenant.id, channel_id="C1", account_id=member.id, platform="slack", thread_id="T1"
    )
    async with db_session_factory() as session:
        with pytest.raises(UserFacingError, match="changed owner"):
            await resolve(session, context=here, default=_DEFAULT)


async def test_an_owner_thread_never_routes_a_caller_with_no_account(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.scope import ScopeContext
    from daimon.core.stores.scoped_config_read import resolve

    tenant = await _seed(db_session)
    member = await make_account(db_session, tenant=tenant)
    await _owned_copy(db_session, tenant, member.id)
    await make_thread_session(
        db_session, tenant=tenant, account=member, platform="slack", thread_id="T1", channel_id="C1"
    )
    await db_session.commit()
    async with db_session_factory.begin() as session:
        await _switch(session, tenant.id, caller=_owner_caller(member.id), destination=_COPY)
    anonymous = ScopeContext(tenant_id=tenant.id, channel_id="C1", platform="slack", thread_id="T1")
    async with db_session_factory() as session:
        config = await resolve(session, context=anonymous, default=_DEFAULT)
    # Fail closed: the channel's own agent, never the private copy.
    assert config.responder_ma_agent_id != "agt_copy" and config.thread_binding_id is None
