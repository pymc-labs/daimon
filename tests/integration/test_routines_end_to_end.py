"""End-to-end routines runtime: scheduler tick -> fake MA -> record_result.

This is the canonical proof that the routines integration coverage closes:

1. ``last_result_tail`` is populated on success.
2. ``last_error`` is populated on failure (``session.error`` event).
3. The advisory lock blocks a second concurrent scheduler.

The tests take a real ``AsyncEngine`` bound to the test Postgres from the
``schema_engine`` fixture, an own pooled engine on the worker's test schema. The
scheduler's ``run`` is invoked through its ``_engine_override`` and
``_anthropic_factory`` test seams so production wiring is exercised
end-to-end without touching real network or the live Anthropic API.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from decimal import Decimal

import httpx
import pytest
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_retry_status_terminal import (
    BetaManagedAgentsRetryStatusTerminal,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_error_event import (
    BetaManagedAgentsSessionErrorEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from anthropic.types.beta.sessions.beta_managed_agents_unknown_error import (
    BetaManagedAgentsUnknownError,
)
from daimon.adapters.scheduler.main import (
    _acquire_advisory_lock,  # pyright: ignore[reportPrivateUsage]  # named test seam for advisory-lock contention
)
from daimon.adapters.scheduler.main import run as scheduler_run
from daimon.core.config import Settings, load_turn_settings
from daimon.core.db import build_engine
from daimon.core.stores import tenant_ledger
from daimon.core.stores.routines import create_routine, get_routine
from daimon.core.turn import driver as turn_driver
from daimon.core.turn.io import LegacyTurnIO, MuxTurnIO, TurnIO
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    json_body,
    list_response,
    make_fake_memory_store_handler,
    send_events_response,
    sse_response,
)
from mux.testing.drivers.anthropic.ma_models import ma_model_config, ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def reset_structlog() -> Iterator[None]:
    """`scheduler_run` configures structlog for the whole process; restore the
    defaults so a later debug-level `capture_logs` in the same worker still sees
    its events."""
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def turn_io_spy(monkeypatch: pytest.MonkeyPatch) -> list[TurnIO]:
    """Every turn I/O the driver built during the test, in order.

    The headless runner reaches ``turn_io`` through the driver, so wrapping the
    driver's binding records which implementation a fire really ran on.
    """
    built: list[TurnIO] = []
    real = turn_driver.turn_io  # pyright: ignore[reportPrivateImportUsage]

    def spy(*args: object, **kwargs: object) -> TurnIO:
        io = real(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        built.append(io)
        return io

    monkeypatch.setattr(turn_driver, "turn_io", spy)
    return built


def _assert_ran_on_configured_path(built: list[TurnIO], *, fires: int) -> None:
    """Each fire built one turn I/O of the configured path's kind, and no other."""
    path = load_turn_settings().path
    assert path == os.environ.get("DAIMON_TURN__PATH", "legacy"), "settings must follow the env leg"
    assert len(built) == fires, f"expected {fires} turn I/O for {fires} fire(s), got {built!r}"
    for io in built:
        if path == "mux":
            assert type(io) is MuxTurnIO, f"mux leg ran {type(io).__name__}"
        else:
            assert isinstance(io, LegacyTurnIO), f"legacy leg ran {type(io).__name__}"


_NOW = dt.datetime(2026, 5, 8, 12, 0, 0, tzinfo=dt.UTC)


def _test_dsn() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for integration tests")
    return url


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_settings()`` requires database + anthropic + mcp.jwt_secret."""
    monkeypatch.setenv("DAIMON_DATABASE__URL", _test_dsn())
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "test-key")
    monkeypatch.setenv("DAIMON_MCP__JWT_SECRET", "x" * 32)
    # public_url makes _validate_mcp_settings pass and drives create_session
    # down the vault-attach branch. Set it explicitly so the test does not
    # depend on a local .env (absent in CI).
    monkeypatch.setenv("DAIMON_MCP__PUBLIC_URL", "https://mcp.test.example")
    # Use a unique advisory-lock key per test so they don't contend with
    # each other or with any other scheduler on this DB.
    monkeypatch.setenv("DAIMON_SCHEDULER__ADVISORY_LOCK_KEY", str(uuid.uuid4().int >> 65))


def _anthropic_factory(
    *,
    agents: list[BetaManagedAgentsAgent],
    environments: list[BetaEnvironment],
    events: list[BetaManagedAgentsSessionEvent],
) -> Callable[[Settings], Awaitable[AsyncAnthropic]]:
    """Serve a routine fire through the real SDK and an isolated MA transport."""
    router = MARouter()
    router.add_agent_list(*(agent for agent in agents if agent.archived_at is None))
    router.add_environment_list(*environments)
    for agent in agents:
        router.add_agent(agent)
    for environment in environments:
        router.add_environment(environment)

    memory_handler = make_fake_memory_store_handler()
    router.add("POST", r"/v1/memory_stores", lambda request, _match: memory_handler(request))
    router.add("GET", r"/v1/vaults", lambda _request, _match: list_response([]))
    router.add(
        "POST",
        r"/v1/vaults",
        lambda _request, _match: httpx.Response(200, json={"id": "vault_test"}),
    )
    router.add(
        "POST",
        r"/v1/vaults/vault_test/credentials",
        lambda _request, _match: httpx.Response(200, json={}),
    )

    def create_session(request: httpx.Request, _match: object) -> httpx.Response:
        body = json_body(request)
        agent = next(agent for agent in agents if agent.id == body["agent"])
        session = ma_session(
            id="ses_test",
            agent=agent,
            environment_id=body["environment_id"],
            metadata=body.get("metadata", {}),
            resources=body.get("resources", []),
            vault_ids=body.get("vault_ids", []),
        )
        router.add_session(session)
        return httpx.Response(200, json=session.model_dump(mode="json"))

    router.add("POST", r"/v1/sessions", create_session)
    router.add("GET", r"/v1/sessions", lambda _request, _match: list_response([]))
    router.add(
        "POST",
        r"/v1/sessions/ses_test/events",
        lambda _request, _match: send_events_response(),
    )
    router.add(
        "GET",
        r"/v1/sessions/ses_test/events/stream",
        lambda _request, _match: sse_response([event.model_dump(mode="json") for event in events]),
    )
    router.add(
        "GET",
        r"/v1/sessions/ses_test/events",
        lambda _request, _match: list_response([event.model_dump(mode="json") for event in events]),
    )
    transport = ScriptedTransport(router=router)

    async def factory(_settings: Settings) -> AsyncAnthropic:
        return transport.client(max_retries=0)

    return factory


def _build_fake_anthropic_factory(
    events: list[BetaManagedAgentsSessionEvent],
    *,
    tenant_id: uuid.UUID,
    agent_id: str = "agent_x",
    agent_name: str = "daimon",
    env_id: str = "env_default",
) -> Callable[[Settings], Awaitable[AsyncAnthropic]]:
    now_dt = dt.datetime.now(dt.UTC)
    return _anthropic_factory(
        agents=[
            ma_agent(
                id=agent_id,
                name=agent_name,
                model=ma_model_config(speed="standard"),
                tenant_id=tenant_id,
                created_at=now_dt,
            )
        ],
        environments=[
            ma_environment(
                id=env_id, name="default", tenant_id=tenant_id, created_at=now_dt.isoformat()
            )
        ],
        events=events,
    )


async def test_end_to_end_tick_populates_last_result_tail(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    turn_io_spy: list[TurnIO],
) -> None:
    engine, sm, tenant_id = schema_engine
    _set_required_env(monkeypatch)

    # Seed one due routine.
    async with sm() as s, s.begin():
        row = await create_routine(
            s,
            created_by_user_id="u1",
            agent_id="agent_x",
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone_="UTC",
            trigger_message="ROUTINE_FIRED",
            next_fire_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
            tenant_id=tenant_id,
        )

    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="ROUTINE_FIRED")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]

    rc = await scheduler_run(
        ["--once"],
        _anthropic_factory=_build_fake_anthropic_factory(events, tenant_id=tenant_id),
        _engine_override=engine,
    )
    assert rc == 0, "scheduler --once should exit 0 on success"
    _assert_ran_on_configured_path(turn_io_spy, fires=1)

    async with sm() as s:
        refreshed = await get_routine(s, row.id, tenant_id=tenant_id)
    assert refreshed is not None
    assert refreshed.last_result_tail == "ROUTINE_FIRED", (
        "tail should carry the agent.message text from the fake MA"
    )
    assert refreshed.last_error is None, "no error should be recorded on success"
    assert refreshed.last_fired_at is not None, "claim sets last_fired_at"


async def test_failure_records_last_error(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    turn_io_spy: list[TurnIO],
) -> None:
    engine, sm, tenant_id = schema_engine
    _set_required_env(monkeypatch)

    async with sm() as s, s.begin():
        row = await create_routine(
            s,
            created_by_user_id="u1",
            agent_id="agent_x",
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone_="UTC",
            trigger_message="will-fail",
            next_fire_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
            tenant_id=tenant_id,
        )

    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsSessionErrorEvent(
            id="evt_err_1",
            type="session.error",
            processed_at=_NOW,
            error=BetaManagedAgentsUnknownError(
                type="unknown_error",
                message="upstream boom",
                retry_status=BetaManagedAgentsRetryStatusTerminal(type="terminal"),
            ),
        ),
    ]

    rc = await scheduler_run(
        ["--once"],
        _anthropic_factory=_build_fake_anthropic_factory(events, tenant_id=tenant_id),
        _engine_override=engine,
    )
    assert rc == 0, "scheduler exits 0 even when a fire fails — error is per-routine"
    _assert_ran_on_configured_path(turn_io_spy, fires=1)

    async with sm() as s:
        refreshed = await get_routine(s, row.id, tenant_id=tenant_id)
    assert refreshed is not None
    assert refreshed.last_result_tail is None, "no tail on failure"
    assert refreshed.last_error is not None, "last_error must be recorded"
    # Post-19-09: headless_runner delegates its drain to the core turn
    # driver, which raises the failed turn's own TurnError (kind-prefixed,
    # e.g. "upstream: ...") rather than the old bespoke
    # RuntimeError("session.error: ..."). scheduler.py's catch-all records
    # `f"{type(err).__name__}: {err}"`.
    assert refreshed.last_error.startswith("TurnError: upstream:"), (
        f"unexpected error format: {refreshed.last_error!r}"
    )


async def test_advisory_lock_blocks_second_run(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    turn_io_spy: list[TurnIO],
) -> None:
    engine, sm, tenant_id = schema_engine
    _set_required_env(monkeypatch)
    lock_key = int(os.environ["DAIMON_SCHEDULER__ADVISORY_LOCK_KEY"])

    # Seed a due routine — its last_fired_at must remain unchanged because
    # the second `run` should bail before it can claim anything.
    async with sm() as s, s.begin():
        row = await create_routine(
            s,
            created_by_user_id="u1",
            agent_id="agent_x",
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone_="UTC",
            trigger_message="should-not-fire",
            next_fire_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
            tenant_id=tenant_id,
        )

    # Hold the advisory lock from a separate, raw engine — independent of
    # the per-test-schema engine, since advisory locks are global to the DB.
    holder_engine = build_engine(_test_dsn())
    holder_conn = await _acquire_advisory_lock(holder_engine, lock_key)
    assert holder_conn is not None, "holder must acquire the lock first"

    try:
        rc = await scheduler_run(
            ["--once"],
            _anthropic_factory=_build_fake_anthropic_factory([], tenant_id=tenant_id),
            _engine_override=engine,
        )
        assert rc == 1, "second scheduler must exit non-zero when lock is held"
        _assert_ran_on_configured_path(turn_io_spy, fires=0)

        async with sm() as s:
            refreshed = await get_routine(s, row.id, tenant_id=tenant_id)
        assert refreshed is not None
        assert refreshed.last_fired_at is None, (
            "no claim should happen when scheduler bails on lock contention"
        )
    finally:
        await holder_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
        await holder_conn.close()
        await holder_engine.dispose()


def _build_archived_agent_factory(
    events: list[BetaManagedAgentsSessionEvent],
    *,
    tenant_id: uuid.UUID,
    stale_agent_id: str,
    fresh_agent_id: str,
    agent_name: str = "daimon",
    env_id: str = "env_default",
) -> Callable[[Settings], Awaitable[AsyncAnthropic]]:
    now_dt = dt.datetime.now(dt.UTC)
    archived_agent = ma_agent(
        id=stale_agent_id,
        name=agent_name,
        model=ma_model_config(speed="standard"),
        tenant_id=tenant_id,
        created_at=now_dt,
        archived_at=now_dt,
    )
    fresh_agent = ma_agent(
        id=fresh_agent_id,
        name=agent_name,
        model=ma_model_config(speed="standard"),
        tenant_id=tenant_id,
        created_at=now_dt,
    )
    return _anthropic_factory(
        agents=[archived_agent, fresh_agent],
        environments=[
            ma_environment(
                id=env_id, name="default", tenant_id=tenant_id, created_at=now_dt.isoformat()
            )
        ],
        events=events,
    )


async def test_fire_heals_archived_agent_id(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    turn_io_spy: list[TurnIO],
) -> None:
    """Acceptance test: routine has an archived ``agent_id``;
    fire resolves a fresh id via tag lookup, persists the heal, and completes
    the turn successfully.
    """
    engine, sm, tenant_id = schema_engine
    _set_required_env(monkeypatch)

    async with sm() as s, s.begin():
        row = await create_routine(
            s,
            created_by_user_id="u1",
            agent_id="ag_stale",
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone_="UTC",
            trigger_message="HEALED",
            next_fire_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
            tenant_id=tenant_id,
        )

    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg_1",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="HEALED")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle_1",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]

    rc = await scheduler_run(
        ["--once"],
        _anthropic_factory=_build_archived_agent_factory(
            events,
            tenant_id=tenant_id,
            stale_agent_id="ag_stale",
            fresh_agent_id="ag_fresh",
        ),
        _engine_override=engine,
    )
    assert rc == 0, "scheduler --once should exit 0 on success"
    _assert_ran_on_configured_path(turn_io_spy, fires=1)

    async with sm() as s:
        refreshed = await get_routine(s, row.id, tenant_id=tenant_id)
    assert refreshed is not None
    assert refreshed.agent_id == "ag_fresh", (
        "resolver should heal the archived agent_id to the fresh tag-lookup match"
    )
    assert refreshed.last_result_tail == "HEALED", (
        "fire should complete the turn after the resolver heals the stale id"
    )
    assert refreshed.last_error is None, "no error should be recorded on heal-then-succeed"


def _build_two_tenant_fake_anthropic_factory(
    *,
    tenant_id_a: uuid.UUID,
    tenant_id_b: uuid.UUID,
    agent_id_a: str = "agent_a",
    agent_id_b: str = "agent_b",
    env_id_a: str = "env_a",
    env_id_b: str = "env_b",
) -> Callable[[Settings], Awaitable[AsyncAnthropic]]:
    """Both tenants share one SDK transport and resolve their own MA resources."""
    now_dt = dt.datetime.now(dt.UTC)
    events: list[BetaManagedAgentsSessionEvent] = [
        BetaManagedAgentsAgentMessageEvent(
            id="evt_msg",
            type="agent.message",
            processed_at=_NOW,
            content=[BetaManagedAgentsTextBlock(type="text", text="FIRED_OK")],
        ),
        BetaManagedAgentsSessionStatusIdleEvent(
            id="evt_idle",
            type="session.status_idle",
            processed_at=_NOW,
            stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        ),
    ]
    return _anthropic_factory(
        agents=[
            ma_agent(
                id=agent_id_a,
                name="daimon",
                model=ma_model_config(speed="standard"),
                tenant_id=tenant_id_a,
                created_at=now_dt,
            ),
            ma_agent(
                id=agent_id_b,
                name="daimon",
                model=ma_model_config(speed="standard"),
                tenant_id=tenant_id_b,
                created_at=now_dt,
            ),
        ],
        environments=[
            ma_environment(
                id=env_id_a, name="default", tenant_id=tenant_id_a, created_at=now_dt.isoformat()
            ),
            ma_environment(
                id=env_id_b, name="default", tenant_id=tenant_id_b, created_at=now_dt.isoformat()
            ),
        ],
        events=events,
    )


async def test_end_to_end_tick_fires_each_routine_under_own_tenant(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    turn_io_spy: list[TurnIO],
) -> None:
    """SCHED-01 end-to-end proof: each routine fires under its own tenant_id.

    Two tenants, one routine each. A single --once tick resolves agent and
    environment per row.tenant_id (not a shared boot singleton), producing
    each routine's trigger_message in its own last_result_tail.
    """
    engine, sm, tenant_id_a = schema_engine
    _set_required_env(monkeypatch)

    # Seed a second tenant in the test body (fixture seeds the first).
    async with sm() as s, s.begin():
        tenant_b_row = await make_tenant(s, platform="discord", workspace_id="e2e-guild-b")
        tenant_id_b = tenant_b_row.id
        # Positive balance so the admission gate (is_over_balance) does not block tenant B's fire.
        await tenant_ledger.insert_entry(
            s,
            tenant_id=tenant_id_b,
            delta_usd=Decimal("100"),
            reason="test-seed",
            idempotency_key=f"test-seed:{tenant_id_b}",
        )

    async with sm() as s, s.begin():
        row_a = await create_routine(
            s,
            created_by_user_id="ua",
            agent_id="agent_a",
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone_="UTC",
            trigger_message="FIRED_A",
            next_fire_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
            tenant_id=tenant_id_a,
        )
        row_b = await create_routine(
            s,
            created_by_user_id="ub",
            agent_id="agent_b",
            agent_name="daimon",
            cron_expr="* * * * *",
            timezone_="UTC",
            trigger_message="FIRED_B",
            next_fire_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5),
            tenant_id=tenant_id_b,
        )

    rc = await scheduler_run(
        ["--once"],
        _anthropic_factory=_build_two_tenant_fake_anthropic_factory(
            tenant_id_a=tenant_id_a,
            tenant_id_b=tenant_id_b,
        ),
        _engine_override=engine,
    )
    assert rc == 0, "scheduler --once should exit 0 on success"
    _assert_ran_on_configured_path(turn_io_spy, fires=2)

    async with sm() as s:
        refreshed_a = await get_routine(s, row_a.id, tenant_id=tenant_id_a)
        refreshed_b = await get_routine(s, row_b.id, tenant_id=tenant_id_b)

    assert refreshed_a is not None
    assert refreshed_a.last_result_tail == "FIRED_OK", (
        f"tenant_a routine must complete with tail 'FIRED_OK', got {refreshed_a.last_result_tail!r}"
    )
    assert refreshed_a.last_error is None, (
        f"tenant_a routine must have no error, got {refreshed_a.last_error!r}"
    )

    assert refreshed_b is not None
    assert refreshed_b.last_result_tail == "FIRED_OK", (
        f"tenant_b routine must complete with tail 'FIRED_OK', got {refreshed_b.last_result_tail!r}"
    )
    assert refreshed_b.last_error is None, (
        f"tenant_b routine must have no error, got {refreshed_b.last_error!r}"
    )
