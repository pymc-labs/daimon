"""Moving an agent's rows to its converted twin, and converting a Daimon record back to Claude."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core import agent_convert
from daimon.core._models import ThreadLease
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.agent_convert import convert_agent_to_anthropic, convert_agent_to_daimon
from daimon.core.agent_ownership import record_agent_creator
from daimon.core.anthropic_guard import AnthropicUnconfigured
from daimon.core.authz import Subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_CONVERTED_FROM
from daimon.core.errors import UserFacingError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.stores import agent_owners, routines, thread_sessions
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.agent_rekey import (
    KEPT,
    REKEYED,
    agent_id_columns,
    open_agent_work,
    rekey_agent,
)
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.testing.factories import (
    make_account,
    make_routine,
    make_task_continuation,
    make_tenant,
    make_thread_session,
)
from daimon.testing.ma_models import ma_agent
from mux.errors import ProviderError
from mux.testing.drivers.anthropic.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ADMIN = Subject(is_admin=True)


@pytest.fixture(autouse=True)
def openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DEFAULT_KEY_ENV.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(DEFAULT_KEY_ENV["openai"], "test-key")


@asynccontextmanager
async def _no_lock(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    # Test sessions share one connection, so the lock's rollback on a simulated
    # crash would undo the committed writes a real crash leaves behind.
    yield


class _FakeMA:
    """Agent CRUD with archive and a count of creates."""

    def __init__(self) -> None:
        self.state = FakeMAState()
        self.creates = 0
        self._crud = make_fake_ma_handler(self.state)

    def seed(self, agent_id: str, name: str, tenant_id: uuid.UUID) -> None:
        agent = ma_agent(id=agent_id, name=name, tenant_id=tenant_id, system="Analyse.")
        self.state.agents[agent_id] = agent.model_dump(mode="json")

    def live(self) -> list[dict[str, object]]:
        return [a for a in self.state.agents.values() if a.get("archived_at") is None]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/agents":
            archived = request.url.params.get("include_archived") == "true"
            data = list(self.state.agents.values()) if archived else self.live()
            return httpx.Response(200, json={"data": data, "has_more": False})
        if request.method == "POST" and path.endswith("/archive"):
            agent = self.state.agents[path.split("/")[3]]
            agent["archived_at"] = "2026-11-10T00:00:00Z"
            return httpx.Response(200, json=agent)
        if request.method == "POST" and path == "/v1/agents":
            self.creates += 1
        return self._crud(request)

    def client(self) -> AsyncAnthropic:
        return build_fake_anthropic(self.handle)


def test_every_agent_id_column_is_moved_or_kept_on_purpose() -> None:
    """Contract: each agent-id column in the schema is re-keyed or kept with a reason."""
    columns = agent_id_columns()
    assert not REKEYED & KEPT.keys(), f"listed twice: {REKEYED & KEPT.keys()}"
    assert columns - REKEYED - KEPT.keys() == set(), "new agent-id columns need a decision"
    assert (REKEYED | KEPT.keys()) - columns == set(), "listed columns must exist"
    assert all(KEPT.values()), "every kept column says why"


async def test_rekey_moves_only_this_agent_and_is_repeatable(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Owner, routine and thread follow the agent; another agent's rows stay put."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    mine = await make_routine(db_session, tenant=tenant, agent_id="agent_old")
    other = await make_routine(db_session, tenant=tenant, agent_id="agent_other")
    thread = await make_thread_session(db_session, tenant=tenant, ma_agent_id="agent_old")
    await db_session.commit()
    await record_agent_creator(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_old"),
        account_id=account.id,
    )

    async with db_session_factory() as session, session.begin():
        moved = await rekey_agent(session, tenant_id=tenant.id, old_id="agent_old", new_id="dm_x")
    async with db_session_factory() as session, session.begin():
        again = await rekey_agent(session, tenant_id=tenant.id, old_id="agent_old", new_id="dm_x")

    assert moved >= 3, f"owner, routine and thread move: {moved}"
    assert again == 0, "a re-run moves nothing"
    async with db_session_factory() as session:
        moved_routine = await routines.get_routine(session, mine.id, tenant_id=tenant.id)
        kept_routine = await routines.get_routine(session, other.id, tenant_id=tenant.id)
        moved_thread = await thread_sessions.get_thread_session_by_id(session, id=thread.id)
        owner = await agent_owners.owner_of(
            session, tenant.id, derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="dm_x")
        )
    assert moved_routine is not None and moved_routine.agent_id == "dm_x", "routine follows"
    assert kept_routine is not None and kept_routine.agent_id == "agent_other", "other stays"
    assert moved_thread is not None and moved_thread.ma_agent_id == "dm_x", "thread follows"
    assert owner == account.id, "owner follows"


async def test_round_trip_keeps_name_owner_threads_routines_and_rules(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Claude to Daimon and back: one live agent under the name, with everything it had."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_rules={"analyst": AgentRule(invoker_role_ids=("111",))}),
    )
    routine = await make_routine(db_session, tenant=tenant, agent_id="agent_ma1")
    thread = await make_thread_session(db_session, tenant=tenant, ma_agent_id="agent_ma1")
    await db_session.commit()
    await record_agent_creator(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_ma1"),
        account_id=account.id,
    )
    fake = _FakeMA()
    fake.seed("agent_ma1", "analyst", tenant.id)

    record = await convert_agent_to_daimon(
        fake.client(), db_session_factory, tenant_id=tenant.id, name="analyst", subject=_ADMIN
    )
    back = await convert_agent_to_anthropic(
        fake.client(),
        db_session_factory,
        tenant_id=tenant.id,
        name="analyst",
        subject=_ADMIN,
        public_url=None,
    )

    final = back.agent.id
    assert not final.startswith("dm_"), f"the agent is on Claude again: {final}"
    assert [a["id"] for a in fake.live()] == [final], "one live Claude agent"
    assert back.agent.name == "analyst", "the name carries"
    assert back.agent.metadata[MA_METADATA_KEY_CONVERTED_FROM] == record.agent.id, "marked"
    with pytest.raises(ProviderError):
        await SelfHostedAgents(db_session_factory).resolve(
            resource_scope(tenant_id=str(tenant.id)), "analyst"
        )
    async with db_session_factory() as session:
        moved_routine = await routines.get_routine(session, routine.id, tenant_id=tenant.id)
        moved_thread = await thread_sessions.get_thread_session_by_id(session, id=thread.id)
        owner = await agent_owners.owner_of(
            session, tenant.id, derive_agent_uuid(tenant_id=tenant.id, ma_agent_id=final)
        )
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert moved_routine is not None and moved_routine.agent_id == final, "routine carries"
    assert moved_thread is not None and moved_thread.ma_agent_id == final, "thread carries"
    assert owner == account.id, "owner carries"
    assert "analyst" in policy.agent_rules, "the name's rule survives"


async def test_rerun_after_a_crash_makes_one_claude_agent(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the create and before the move: the re-run adopts the marked agent."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    fake = _FakeMA()
    fake.seed("agent_ma1", "analyst", tenant.id)
    await convert_agent_to_daimon(
        fake.client(), db_session_factory, tenant_id=tenant.id, name="analyst", subject=_ADMIN
    )
    real = agent_convert.rekey_agent

    async def crash(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("crash")

    monkeypatch.setattr(agent_convert, "agent_name_lock", _no_lock)
    monkeypatch.setattr(agent_convert, "rekey_agent", crash)
    with pytest.raises(RuntimeError):
        await convert_agent_to_anthropic(
            fake.client(),
            db_session_factory,
            tenant_id=tenant.id,
            name="analyst",
            subject=_ADMIN,
            public_url=None,
        )
    monkeypatch.setattr(agent_convert, "rekey_agent", real)
    await convert_agent_to_anthropic(
        fake.client(),
        db_session_factory,
        tenant_id=tenant.id,
        name="analyst",
        subject=_ADMIN,
        public_url=None,
    )

    assert fake.creates == 1, f"exactly one Claude agent is created: {fake.creates}"
    assert len(fake.live()) == 1, "one live agent under the name"


async def test_pending_wake_blocks_the_move(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A queued wake for the record refuses the conversion before any Claude call."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    fake = _FakeMA()
    fake.seed("agent_ma1", "analyst", tenant.id)
    record = await convert_agent_to_daimon(
        fake.client(), db_session_factory, tenant_id=tenant.id, name="analyst", subject=_ADMIN
    )
    await make_task_continuation(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="1",
        thread_id="2",
        requester_account_id=account.id,
        requester_external_user_id="3",
        target_ma_agent_id=record.agent.id,
        target_name="analyst",
        reason="timer",
        idempotency_key=uuid.uuid4(),
    )
    await db_session.commit()

    with pytest.raises(UserFacingError, match="still working"):
        await convert_agent_to_anthropic(
            fake.client(),
            db_session_factory,
            tenant_id=tenant.id,
            name="analyst",
            subject=_ADMIN,
            public_url=None,
        )
    assert fake.creates == 0, "nothing is created while work is open"


@pytest.mark.parametrize("held", [False, True])
async def test_a_running_reply_blocks_the_move(db_session: AsyncSession, held: bool) -> None:
    """A held, unexpired lease on the agent's live thread is open work (B-LB1)."""
    import datetime as dt

    tenant = await make_tenant(db_session)
    thread = await make_thread_session(db_session, tenant=tenant, ma_agent_id="agent_old")
    now = dt.datetime.now(dt.UTC)
    lease = {
        "slot": {"tenant_id": str(tenant.id), "thread_id": thread.thread_id},
        "holder": "worker-1",
        "turn_id": "turn-1",
        "fence": 1,
        "acquired_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(minutes=5 if held else -5)).isoformat(),
    }
    db_session.add(
        ThreadLease(
            tenant_id=tenant.id,
            platform=thread.platform,
            channel_id="c1",
            thread_id=thread.thread_id,
            last_fence=1,
            active=lease,
        )
    )
    await db_session.flush()

    open_work = await open_agent_work(db_session, tenant_id=tenant.id, agent_id="agent_old")

    assert open_work == (1 if held else 0), f"only a live lease is open work: {open_work}"


async def test_keyless_install_refuses_the_move(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """With no Anthropic key there is nowhere to move to."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    with pytest.raises(AnthropicUnconfigured):
        await convert_agent_to_anthropic(
            None,
            db_session_factory,
            tenant_id=tenant.id,
            name="analyst",
            subject=_ADMIN,
            public_url=None,
        )


async def test_rerun_to_daimon_after_a_crash_replays_the_record(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record's create carries a receipt key, so a re-run reuses it."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    fake = _FakeMA()
    fake.seed("agent_ma1", "analyst", tenant.id)
    real = agent_convert.rekey_agent
    monkeypatch.setattr(agent_convert, "agent_name_lock", _no_lock)

    async def crash(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("crash")

    monkeypatch.setattr(agent_convert, "rekey_agent", crash)
    with pytest.raises(RuntimeError):
        await convert_agent_to_daimon(
            fake.client(), db_session_factory, tenant_id=tenant.id, name="analyst", subject=_ADMIN
        )
    first = await SelfHostedAgents(db_session_factory).resolve(
        resource_scope(tenant_id=str(tenant.id)), "analyst"
    )
    monkeypatch.setattr(agent_convert, "rekey_agent", real)
    converted = await convert_agent_to_daimon(
        fake.client(), db_session_factory, tenant_id=tenant.id, name="analyst", subject=_ADMIN
    )

    assert converted.agent.id == first.ref.id, "the re-run reuses the first record"
    assert fake.live() == [], "the Claude twin is archived once the move finishes"
