"""Self-helpers: a `[self]` roster runs as parallel copies on a provider without multiagent."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core import helpers
from daimon.core.helpers import (
    SELF_BUDGET_SPENT,
    SELF_COST_UNKNOWN,
    _Desk,  # pyright: ignore[reportPrivateUsage]
    asks_copies,
    helper_message,
)
from daimon.core.self_helpers import (
    TURN_BUDGET_USD,
    is_self_roster,
    self_helper_thread_key,
    translates_self_roster,
)
from daimon.core.stores import helper_asks
from daimon.core.stores.domain import (
    BackendAgent,
    HelperAskRow,
    Role,
    TurnOriginRow,
    helper_thread_key,
)
from daimon.core.stores.turn_origins import create_origin
from daimon.core.turn.admission import Admission, AdmitRequest
from daimon.core.turn.runtime_manifest import (
    classify_source_obligations,
    preview_source_drops,
    source_translations,
)
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.turn_fakes import deps_for
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Provider
from mux.errors import UnsupportedCapability
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .turn.source_projection_support import captured_source

NOW = datetime(2026, 11, 2, tzinfo=UTC)
LEAD = "agent_lead"


def test_a_roster_of_only_the_agent_is_a_self_roster() -> None:
    assert is_self_roster([{"type": "self"}], LEAD)
    assert is_self_roster([{"type": "agent", "id": LEAD}], LEAD)
    assert is_self_roster([LEAD], LEAD)
    assert is_self_roster([SimpleNamespace(type="agent", id=LEAD)], LEAD)
    assert not is_self_roster([{"type": "agent", "id": "agent_other"}], LEAD)
    assert not is_self_roster([{"type": "self"}, {"type": "self"}], LEAD)


def test_only_providers_without_multiagent_translate() -> None:
    assert not translates_self_roster("anthropic.managed_agents")
    assert translates_self_roster("openai.persistent_workspace") and translates_self_roster(
        "gemini.inline_reuse"
    )


def test_each_copy_has_its_own_conversation() -> None:
    keys = {self_helper_thread_key("1.2", 1), self_helper_thread_key("1.2", 2)}
    assert keys == {"1.2~helper~self~1", "1.2~helper~self~2"}
    assert helper_thread_key("1.2", LEAD) not in keys


def _admission(profile: str | None, agents: list[object]) -> Admission:
    revision = None if profile is None else SimpleNamespace(profile=profile)
    agent = SimpleNamespace(id=LEAD, multiagent=SimpleNamespace(agents=agents))
    return cast(Admission, SimpleNamespace(backend_revision=revision, agent=agent))


def test_copies_replace_a_native_self_roster_only_off_managed_agents() -> None:
    assert asks_copies(_admission("openai.persistent_workspace", [{"type": "self"}]))
    assert asks_copies(_admission("gemini.inline_reuse", [{"type": "agent", "id": LEAD}]))
    assert not asks_copies(_admission("anthropic.managed_agents", [{"type": "self"}])), (
        "runs natively"
    )
    assert not asks_copies(_admission(None, [{"type": "self"}])), "Managed Agents runs it"
    assert not asks_copies(
        _admission("openai.persistent_workspace", [{"type": "agent", "id": "agent_b"}])
    )


def test_a_copy_is_told_it_shares_no_files() -> None:
    ask = HelperAskRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        origin_id=uuid.uuid4(),
        helper_agent_id=LEAD,
        helper_name="Your copy",
        message="check the second half",
        status="running",
        reply=None,
        created_at=NOW,
        finished_at=None,
    )
    copy = helper_message("lead", ask, "https://daimon.example", copy=True)
    assert "You are a copy of lead" in copy and "files move only by link" in copy
    assert "copy of" not in helper_message("lead", ask, "https://daimon.example")


def test_a_copys_turn_uses_its_own_key() -> None:
    key = self_helper_thread_key("1.2", 2)
    origin = TurnOriginRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        platform="slack",
        parent_channel_id="C1",
        thread_id="1.2",
        responder_ma_agent_id=LEAD,
        responder_name="lead",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        helper_of=uuid.uuid4(),
        helper_key=key,
    )
    assert origin.session_thread_id == key


def _self_roster_source(own: str | None = None) -> BackendAgent:
    source = captured_source()
    assert source.source is not None and source.effective_spec is not None
    agents: list[JsonValue] = [{"type": "agent", "id": own or source.source.ref.id}]
    roster = ExtensionConfig(
        namespace="anthropic.multiagent",
        version=1,
        value={"type": "coordinator", "agents": agents},
    )
    spec = source.effective_spec.model_copy(
        update={"tools": (), "skills": (), "mcp_servers": (), "extensions": (roster,)}
    )
    return source.model_copy(
        update={
            "effective_spec": spec,
            "asks_before_publishing": False,
            "required_capabilities": ("multiagent",),
        }
    )


@pytest.mark.parametrize(
    "provider,model", [("openai", "gpt-6-luna"), ("gemini", "gemini-3.8-flash")]
)
def test_a_switch_records_the_self_roster_as_translated_never_a_loss(
    provider: Provider, model: str
) -> None:
    from daimon.core.channel_backend import compatibility_preview
    from daimon.core.stores.domain import TenantBackendPolicy

    source = _self_roster_source()
    target = preview_source_drops(source, ModelRef(provider=provider, id=model))
    assert "source_definition" not in target.declared_drops
    assert classify_source_obligations(source, target, environment=None) == frozenset()

    preview = compatibility_preview(
        source, provider, TenantBackendPolicy(tenant_id=source.tenant_id, revision="policy")
    )
    assert preview.translated_capabilities == ("multiagent",)
    assert "multiagent" not in preview.unsupported_capabilities
    assert "multiagent" not in preview.declared_drops


def test_only_a_self_roster_off_managed_agents_is_translated() -> None:
    source = _self_roster_source()
    assert source_translations(source, "anthropic.managed_agents") == (), "runs natively"
    other = _self_roster_source("someone-else")
    assert source_translations(other, "openai.persistent_workspace") == ()
    with pytest.raises(UnsupportedCapability) as refused:
        preview_source_drops(other, ModelRef(provider="openai", id="gpt-6-luna"))
    assert "source_definition" in refused.value.missing, "another agent's roster stays a loss"


async def _desk_origin(sm: async_sessionmaker[AsyncSession]) -> TurnOriginRow:
    async with sm.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        now = datetime.now(UTC)
        return await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="slack",
            parent_channel_id="C1",
            thread_id="1.2",
            responder_ma_agent_id=LEAD,
            responder_name="lead",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            sender_foreign=False,
            shared_channel=False,
            expires_at=now + timedelta(minutes=10),
            now=now,
        )


async def _ask_self(sm: async_sessionmaker[AsyncSession], origin: TurnOriginRow, text: str) -> None:
    async with sm.begin() as session:
        await helper_asks.create_ask(
            session,
            tenant_id=origin.tenant_id,
            origin_id=origin.id,
            helper_agent_id=LEAD,
            helper_name="Your copy",
            message=text,
            now=datetime.now(UTC),
        )


def _desk(sm: async_sessionmaker[AsyncSession], origin: TurnOriginRow, runner: Any) -> _Desk:
    return _Desk(
        deps=deps_for(sm=sm),
        request=cast(AdmitRequest, None),
        origin=origin,
        asker_name="lead",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
        confirm_write=None,
        provider_action=None,
        no_anthropic=False,
        self_id=LEAD,
        runner=runner,
    )


async def _settled(
    sm: async_sessionmaker[AsyncSession], origin: TurnOriginRow, count: int
) -> list[HelperAskRow]:
    async with asyncio.timeout(10):
        while True:
            async with sm() as session:
                asks = await helper_asks.list_asks(
                    session, tenant_id=origin.tenant_id, origin_id=origin.id
                )
            done = [ask for ask in asks if ask.status not in ("queued", "running")]
            if len(done) == count:
                return sorted(done, key=lambda ask: ask.created_at)
            await asyncio.sleep(0.02)


@pytest.fixture
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helpers, "POLL_S", 0.02)


@pytest.mark.usefixtures("fast_poll")
async def test_a_fan_out_of_two_copies_runs_in_parallel_with_distinct_keys(
    db_committing_engine: AsyncEngine,
) -> None:
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    origin = await _desk_origin(sm)
    await _ask_self(sm, origin, "first half")
    await _ask_self(sm, origin, "second half")
    lanes: list[str | None] = []
    both = asyncio.Event()

    async def runner(ask: HelperAskRow, cancel: asyncio.Event, lane: str | None):
        lanes.append(lane)
        if len(lanes) == 2:
            both.set()
        # Neither copy answers until both have started: they run at once.
        await asyncio.wait_for(both.wait(), timeout=5)
        return "done", f"answer to {ask.message}", Decimal("0.5")

    desk = _desk(sm, origin, runner)
    serving = asyncio.create_task(desk.serve())
    try:
        finished = await _settled(sm, origin, 2)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await desk.stop()

    assert sorted(lanes, key=str) == ["1.2~helper~self~1", "1.2~helper~self~2"]
    assert [ask.status for ask in finished] == ["done", "done"]
    assert desk.copies_spent == Decimal("1.0")


@pytest.mark.parametrize(
    ("spent", "words"), [(TURN_BUDGET_USD + 1, SELF_BUDGET_SPENT), (None, SELF_COST_UNKNOWN)]
)
@pytest.mark.usefixtures("fast_poll")
async def test_the_spend_ceiling_stops_further_copies(
    db_committing_engine: AsyncEngine, spent: Decimal | None, words: str
) -> None:
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    origin = await _desk_origin(sm)
    await _ask_self(sm, origin, "first")
    calls: list[str] = []

    async def runner(ask: HelperAskRow, cancel: asyncio.Event, lane: str | None):
        calls.append(ask.message)
        return "done", "ok", spent

    desk = _desk(sm, origin, runner)
    serving = asyncio.create_task(desk.serve())
    try:
        await _settled(sm, origin, 1)
        await _ask_self(sm, origin, "second")
        first, second = await _settled(sm, origin, 2)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await desk.stop()

    assert calls == ["first"], "an over-limit or unpriced copy stops the next one"
    assert first.status == "done"
    assert (second.status, second.reply) == ("failed", words), "the refusal says why"


@pytest.mark.usefixtures("fast_poll")
async def test_a_copy_over_the_budget_stops_the_copies_still_running(
    db_committing_engine: AsyncEngine,
) -> None:
    """Parallel copies cannot jointly run past the budget: the rest stop once it is spent."""
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    origin = await _desk_origin(sm)
    await _ask_self(sm, origin, "dear")
    await _ask_self(sm, origin, "slow")
    started = asyncio.Event()

    async def runner(ask: HelperAskRow, cancel: asyncio.Event, lane: str | None):
        if ask.message == "dear":
            await asyncio.wait_for(started.wait(), timeout=5)
            return "done", "ok", TURN_BUDGET_USD + 1
        started.set()
        await asyncio.wait_for(cancel.wait(), timeout=5)
        return "stopped", "stopped", Decimal(0)

    desk = _desk(sm, origin, runner)
    serving = asyncio.create_task(desk.serve())
    try:
        finished = await _settled(sm, origin, 2)
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)
        await desk.stop()

    statuses = {ask.message: ask.status for ask in finished}
    assert statuses == {"dear": "done", "slow": "stopped"}, statuses


def test_running_copies_hold_budget_before_another_starts() -> None:
    """Each running copy holds the dearest copy's cost, so a fan-out cannot start past it."""
    desk = _desk(cast(Any, None), cast(TurnOriginRow, None), None)
    desk.copies_spent = desk.copy_high = Decimal(4)
    assert desk.copies_refusal() is None, "4 spent plus 4 held is under the budget"
    desk.lanes.add("lane-2")
    assert desk.copies_refusal() == SELF_BUDGET_SPENT, "4 spent plus 8 held passes it"
