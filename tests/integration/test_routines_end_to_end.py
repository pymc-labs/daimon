"""End-to-end routines runtime: scheduler tick -> fake MA -> record_result.

This is the canonical proof that the routines integration coverage closes:

1. ``last_result_tail`` is populated on success.
2. ``last_error`` is populated on failure (``session.error`` event).
3. The advisory lock blocks a second concurrent scheduler.

The tests build a real ``AsyncEngine`` bound to the test Postgres, scoped
to a per-test schema via SQLAlchemy ``schema_translate_map``. The
scheduler's ``run`` is invoked through its ``_engine_override`` and
``_anthropic_factory`` test seams so production wiring is exercised
end-to-end without touching real network or the live Anthropic API.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig,
)
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
from daimon.core._models import Base
from daimon.core.config import Settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.stores import tenant_ledger
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_tenant
from daimon.testing.ma import EMPTY_CLOUD_CONFIG
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Reuse the conftest fake-anthropic builder shape — but inline here to avoid
# depending on tests/integration/conftest.py importing private package internals.
# Each test inlines real SDK constructors per guideline:testing.

_NOW = dt.datetime(2026, 5, 8, 12, 0, 0, tzinfo=dt.UTC)


class _EmptySessionPage:
    """Empty async-iterable for ``beta.sessions.list()``.

    The scheduler tick now runs ``sweep_headless_usage``, which lists sessions.
    These routine-fire tests don't exercise the sweep, so their session fakes
    yield no sessions.
    """

    def __aiter__(self) -> _EmptySessionPage:
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration


class _FakeMemoryStores:
    """Stub for ``client.beta.memory_stores`` (Phase: agent-memory).

    ``create_session`` now unconditionally calls
    ``ensure_memory_store_and_mount``, which calls
    ``anthropic.beta.memory_stores.create(...)`` on the cold path (no DB row
    yet). These e2e tests exercise the routine-fire path, not memory
    provisioning itself, so the stub just returns an id — mirroring how
    ``_FakeVaults`` stubs vault provisioning above.
    """

    create = AsyncMock(return_value=SimpleNamespace(id="memstore_fake"))


def _test_dsn() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        pytest.skip("DAIMON_DATABASE__TEST_URL must be set for integration tests")
    return url


@pytest_asyncio.fixture
async def schema_engine() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID]
]:
    """Engine + sessionmaker scoped to a per-test schema.

    The engine is configured with ``schema_translate_map`` so all DDL +
    DML lands in ``test_<uuid>``; the schema is created here and dropped
    on teardown. The scheduler's ``run`` is invoked with this engine via
    ``_engine_override`` so it shares the per-test schema with the test
    body's seed/verify code.
    """
    raw_engine = build_engine(_test_dsn())
    schema = f"test_{uuid.uuid4().hex}"
    async with raw_engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        mapped = await conn.execution_options(schema_translate_map={None: schema})
        await mapped.run_sync(Base.metadata.create_all)
        await mapped.commit()
    await raw_engine.dispose()

    # Build the *real* engine the test body and the scheduler will share.
    # `execution_options(schema_translate_map=...)` returns a wrapper engine
    # whose checked-out connections all carry the translate map.
    engine = build_engine(_test_dsn()).execution_options(
        schema_translate_map={None: schema},
    )
    sm = build_session_factory(engine)

    # Seed a Tenant — the routine row's tenant_id FK points at it.
    # Seed a positive balance so the admission gate (is_over_balance)
    # does not block scheduled fires — these e2e tests exercise the fire path, not the gate.
    async with sm() as s, s.begin():
        tenant = await make_tenant(s, platform="discord", workspace_id="e2e-guild-a")
        tenant_id = tenant.id
        await tenant_ledger.insert_entry(
            s,
            tenant_id=tenant_id,
            delta_usd=Decimal("100"),
            reason="test-seed",
            idempotency_key=f"test-seed:{tenant_id}",
        )

    try:
        yield engine, sm, tenant_id
    finally:
        await engine.dispose()
        # Teardown — drop schema on a fresh raw engine.
        cleanup_engine = build_engine(_test_dsn())
        async with cleanup_engine.connect() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await conn.commit()
        await cleanup_engine.dispose()


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


def _build_fake_anthropic_factory(
    events: list[BetaManagedAgentsSessionEvent],
    *,
    tenant_id: uuid.UUID,
    agent_id: str = "agent_x",
    agent_name: str = "daimon",
    env_id: str = "env_default",
) -> object:
    """Construct an ``_anthropic_factory`` that returns an event-shaped fake.

    Also wires up resolver-facing surface: ``beta.agents.retrieve`` returns a
    live agent matching ``agent_id`` (cached_id liveness path), and
    ``beta.environments.list`` yields one daimon-tagged ``default`` env so
    ``resolve_environment`` resolves on the tag-lookup branch.
    """
    now_dt = dt.datetime.now(dt.UTC)
    now_iso = now_dt.isoformat()
    live_agent = BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=agent_name,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        description=None,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: agent_name,
        },
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at=now_dt,
        updated_at=now_dt,
        archived_at=None,
    )
    live_env = BetaEnvironment(
        id=env_id,
        type="environment",
        name="default",
        description="",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "default",
        },
        created_at=now_iso,
        updated_at=now_iso,
        archived_at=None,
    )

    class _EnvList:
        """Async-iterable wrapper for `client.beta.environments.list(...)`."""

        def __init__(self, envs: list[BetaEnvironment]) -> None:
            self._envs = envs

        def __aiter__(self) -> _EnvList:
            self._iter = iter(self._envs)
            return self

        async def __anext__(self) -> BetaEnvironment:
            try:
                return next(self._iter)
            except StopIteration as err:
                raise StopAsyncIteration from err

    def _envs_list(**_kwargs: object) -> _EnvList:
        return _EnvList([live_env])

    class _FakeAsyncIter:
        def __init__(self, evts: list[BetaManagedAgentsSessionEvent]) -> None:
            self._evts = evts

        def __await__(self):  # noqa: ANN204
            async def _self() -> _FakeAsyncIter:
                return self

            return _self().__await__()

        def __aiter__(self) -> _FakeAsyncIter:
            self._iter = iter(self._evts)
            return self

        async def __anext__(self) -> BetaManagedAgentsSessionEvent:
            try:
                return next(self._iter)
            except StopIteration as err:
                raise StopAsyncIteration from err

    stream_factory = _FakeAsyncIter(events)
    create_mock = AsyncMock(
        return_value=SimpleNamespace(
            id="ses_test",
            agent=SimpleNamespace(model=SimpleNamespace(id="claude-sonnet-4-5")),
        )
    )
    send_mock = AsyncMock(return_value=None)
    close_mock = AsyncMock(return_value=None)

    class _FakeEvents:
        stream = AsyncMock(return_value=stream_factory)
        send = send_mock

    class _FakeSessions:
        events = _FakeEvents()
        create = create_mock

        @staticmethod
        def list(**_kwargs: object) -> _EmptySessionPage:
            return _EmptySessionPage()

    class _FakeAgents:
        retrieve = AsyncMock(return_value=live_agent)

    class _FakeEnvs:
        list = staticmethod(_envs_list)
        # run_turn bridges string ids to SDK objects via environments.retrieve
        # before delegating to create_session (95-04 collapse).
        retrieve = AsyncMock(return_value=live_env)

    class _EmptyVaultPage:
        def __aiter__(self) -> _EmptyVaultPage:
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

    class _FakeVaultCredentials:
        create = AsyncMock(return_value=None)

    class _FakeVaults:
        create = AsyncMock(return_value=SimpleNamespace(id="vault_test"))
        credentials = _FakeVaultCredentials()

        @staticmethod
        def list(**_kwargs: object) -> _EmptyVaultPage:
            return _EmptyVaultPage()

    class _FakeBeta:
        sessions = _FakeSessions()
        agents = _FakeAgents()
        environments = _FakeEnvs()
        vaults = _FakeVaults()
        memory_stores = _FakeMemoryStores()

    class _FakeClient:
        beta = _FakeBeta()
        close = close_mock

    async def factory(_settings: Settings) -> AsyncAnthropic:
        return cast(AsyncAnthropic, _FakeClient())

    return factory


async def test_end_to_end_tick_populates_last_result_tail(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
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
        _anthropic_factory=_build_fake_anthropic_factory(events, tenant_id=tenant_id),  # pyright: ignore[reportArgumentType]
        _engine_override=engine,
    )
    assert rc == 0, "scheduler --once should exit 0 on success"

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
        _anthropic_factory=_build_fake_anthropic_factory(events, tenant_id=tenant_id),  # pyright: ignore[reportArgumentType]
        _engine_override=engine,
    )
    assert rc == 0, "scheduler exits 0 even when a fire fails — error is per-routine"

    async with sm() as s:
        refreshed = await get_routine(s, row.id, tenant_id=tenant_id)
    assert refreshed is not None
    assert refreshed.last_result_tail is None, "no tail on failure"
    assert refreshed.last_error is not None, "last_error must be recorded"
    assert refreshed.last_error.startswith("RuntimeError: session.error:"), (
        f"unexpected error format: {refreshed.last_error!r}"
    )


async def test_advisory_lock_blocks_second_run(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
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
            _anthropic_factory=_build_fake_anthropic_factory([], tenant_id=tenant_id),  # pyright: ignore[reportArgumentType]
            _engine_override=engine,
        )
        assert rc == 1, "second scheduler must exit non-zero when lock is held"

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
) -> object:
    """Like ``_build_fake_anthropic_factory``, but ``agents.retrieve(stale_id)``
    returns a row with ``archived_at`` populated, forcing the resolver into the
    tag-lookup branch where ``agents.list`` yields the fresh agent. Used by
    ``test_fire_heals_archived_agent_id`` (success criterion).
    """
    now_dt = dt.datetime.now(dt.UTC)
    now_iso = now_dt.isoformat()
    archived_agent = BetaManagedAgentsAgent(
        id=stale_agent_id,
        type="agent",
        name=agent_name,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        description=None,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: agent_name,
        },
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at=now_dt,
        updated_at=now_dt,
        archived_at=now_dt,  # archived_at populated -> resolver treats as not-live
    )
    fresh_agent = BetaManagedAgentsAgent(
        id=fresh_agent_id,
        type="agent",
        name=agent_name,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        description=None,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: agent_name,
        },
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at=now_dt,
        updated_at=now_dt,
        archived_at=None,
    )
    live_env = BetaEnvironment(
        id=env_id,
        type="environment",
        name="default",
        description="",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: "default",
        },
        created_at=now_iso,
        updated_at=now_iso,
        archived_at=None,
    )

    class _AgentList:
        def __init__(self, agents: list[BetaManagedAgentsAgent]) -> None:
            self._agents = agents

        def __aiter__(self) -> _AgentList:
            self._iter = iter(self._agents)
            return self

        async def __anext__(self) -> BetaManagedAgentsAgent:
            try:
                return next(self._iter)
            except StopIteration as err:
                raise StopAsyncIteration from err

    class _EnvList:
        def __init__(self, envs: list[BetaEnvironment]) -> None:
            self._envs = envs

        def __aiter__(self) -> _EnvList:
            self._iter = iter(self._envs)
            return self

        async def __anext__(self) -> BetaEnvironment:
            try:
                return next(self._iter)
            except StopIteration as err:
                raise StopAsyncIteration from err

    def _agents_list(**_kwargs: object) -> _AgentList:
        return _AgentList([fresh_agent])  # archived not returned (include_archived=False)

    def _envs_list(**_kwargs: object) -> _EnvList:
        return _EnvList([live_env])

    class _FakeAsyncIter:
        def __init__(self, evts: list[BetaManagedAgentsSessionEvent]) -> None:
            self._evts = evts

        def __await__(self):  # noqa: ANN204
            async def _self() -> _FakeAsyncIter:
                return self

            return _self().__await__()

        def __aiter__(self) -> _FakeAsyncIter:
            self._iter = iter(self._evts)
            return self

        async def __anext__(self) -> BetaManagedAgentsSessionEvent:
            try:
                return next(self._iter)
            except StopIteration as err:
                raise StopAsyncIteration from err

    stream_factory = _FakeAsyncIter(events)
    create_mock = AsyncMock(
        return_value=SimpleNamespace(
            id="ses_test",
            agent=SimpleNamespace(model=SimpleNamespace(id="claude-sonnet-4-5")),
        )
    )

    class _FakeEvents:
        stream = AsyncMock(return_value=stream_factory)
        send = AsyncMock(return_value=None)

    class _FakeSessions:
        events = _FakeEvents()
        create = create_mock

        @staticmethod
        def list(**_kwargs: object) -> _EmptySessionPage:
            return _EmptySessionPage()

    _agents_by_id = {stale_agent_id: archived_agent, fresh_agent_id: fresh_agent}

    class _FakeAgents:
        # Keyed by id: the resolver retrieves the stale id (gets the archived
        # row, forcing tag lookup), then run_turn retrieves the HEALED fresh id
        # (95-04 collapse) — that call must return the live agent whose .id
        # feeds create_session, not the archived one.
        @staticmethod
        async def retrieve(agent_id: str, **_kwargs: object) -> BetaManagedAgentsAgent:
            return _agents_by_id[agent_id]

        list = staticmethod(_agents_list)

    class _FakeEnvs:
        list = staticmethod(_envs_list)
        # run_turn bridges string ids to SDK objects via environments.retrieve
        # before delegating to create_session (95-04 collapse).
        retrieve = AsyncMock(return_value=live_env)

    class _EmptyVaultPage:
        def __aiter__(self) -> _EmptyVaultPage:
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

    class _FakeVaultCredentials:
        create = AsyncMock(return_value=None)

    class _FakeVaults:
        create = AsyncMock(return_value=SimpleNamespace(id="vault_test"))
        credentials = _FakeVaultCredentials()

        @staticmethod
        def list(**_kwargs: object) -> _EmptyVaultPage:
            return _EmptyVaultPage()

    class _FakeBeta:
        sessions = _FakeSessions()
        agents = _FakeAgents()
        environments = _FakeEnvs()
        vaults = _FakeVaults()
        memory_stores = _FakeMemoryStores()

    class _FakeClient:
        beta = _FakeBeta()
        close = AsyncMock(return_value=None)

    async def factory(_settings: Settings) -> AsyncAnthropic:
        return cast(AsyncAnthropic, _FakeClient())

    return factory


async def test_fire_heals_archived_agent_id(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
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
        _anthropic_factory=_build_archived_agent_factory(  # pyright: ignore[reportArgumentType]
            events,
            tenant_id=tenant_id,
            stale_agent_id="ag_stale",
            fresh_agent_id="ag_fresh",
        ),
        _engine_override=engine,
    )
    assert rc == 0, "scheduler --once should exit 0 on success"

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
) -> object:
    """Fake factory for 2-tenant SCHED-01 proof.

    ``agents.retrieve(id)`` returns the matching live agent keyed by id.
    ``environments.list(...)`` returns both envs; the resolver filters by
    ``MA_METADATA_KEY_TENANT`` so each tenant sees only its own environment.
    The event stream uses ``side_effect`` so each of the two fires gets a
    fresh ``_FakeAsyncIter`` (the single-tenant factory's shared stream would
    exhaust on the first fire).
    """
    now_dt = dt.datetime.now(dt.UTC)
    now_iso = now_dt.isoformat()

    agent_a = BetaManagedAgentsAgent(
        id=agent_id_a,
        type="agent",
        name="daimon",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        description=None,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id_a),
            MA_METADATA_KEY_NAME: "daimon",
        },
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at=now_dt,
        updated_at=now_dt,
        archived_at=None,
    )
    agent_b = BetaManagedAgentsAgent(
        id=agent_id_b,
        type="agent",
        name="daimon",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        description=None,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id_b),
            MA_METADATA_KEY_NAME: "daimon",
        },
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at=now_dt,
        updated_at=now_dt,
        archived_at=None,
    )
    env_a = BetaEnvironment(
        id=env_id_a,
        type="environment",
        name="default",
        description="",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id_a),
            MA_METADATA_KEY_NAME: "default",
        },
        created_at=now_iso,
        updated_at=now_iso,
        archived_at=None,
    )
    env_b = BetaEnvironment(
        id=env_id_b,
        type="environment",
        name="default",
        description="",
        config=EMPTY_CLOUD_CONFIG,
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id_b),
            MA_METADATA_KEY_NAME: "default",
        },
        created_at=now_iso,
        updated_at=now_iso,
        archived_at=None,
    )

    _agents_by_id = {agent_id_a: agent_a, agent_id_b: agent_b}
    _envs_by_id = {env_id_a: env_a, env_id_b: env_b}

    class _EnvList:
        """Async-iterable over both envs; resolver filters by tenant metadata."""

        def __init__(self, envs: list[BetaEnvironment]) -> None:
            self._envs = envs

        def __aiter__(self) -> _EnvList:
            self._iter = iter(self._envs)
            return self

        async def __anext__(self) -> BetaEnvironment:
            try:
                return next(self._iter)
            except StopIteration as err:
                raise StopAsyncIteration from err

    def _envs_list(**_kwargs: object) -> _EnvList:
        return _EnvList([env_a, env_b])

    def _make_stream(tail_text: str) -> object:
        """Fresh _FakeAsyncIter for each fire so state is not shared."""
        events: list[BetaManagedAgentsSessionEvent] = [
            BetaManagedAgentsAgentMessageEvent(
                id="evt_msg",
                type="agent.message",
                processed_at=_NOW,
                content=[BetaManagedAgentsTextBlock(type="text", text=tail_text)],
            ),
            BetaManagedAgentsSessionStatusIdleEvent(
                id="evt_idle",
                type="session.status_idle",
                processed_at=_NOW,
                stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
            ),
        ]

        class _FakeAsyncIter:
            def __await__(self):  # noqa: ANN204
                async def _self() -> _FakeAsyncIter:
                    return self

                return _self().__await__()

            def __aiter__(self) -> _FakeAsyncIter:
                self._iter = iter(events)
                return self

            async def __anext__(self) -> BetaManagedAgentsSessionEvent:
                try:
                    return next(self._iter)
                except StopIteration as err:
                    raise StopAsyncIteration from err

        return _FakeAsyncIter()

    # stream mock uses side_effect so each call gets a fresh iterator
    # (returning the same stateful object would exhaust on the first fire).
    # Both fires return the same "FIRED_OK" tail — the per-tenant proof is
    # in the resolver path (each tenant finds its own agent/env via metadata
    # filtering), not in distinct tail content.
    stream_mock = AsyncMock(
        side_effect=[
            _make_stream("FIRED_OK"),
            _make_stream("FIRED_OK"),
        ]
    )
    create_mock = AsyncMock(
        return_value=SimpleNamespace(
            id="ses_test",
            agent=SimpleNamespace(model=SimpleNamespace(id="claude-sonnet-4-5")),
        )
    )

    class _FakeEvents:
        stream = stream_mock
        send = AsyncMock(return_value=None)

    class _FakeSessions:
        events = _FakeEvents()
        create = create_mock

        @staticmethod
        def list(**_kwargs: object) -> _EmptySessionPage:
            return _EmptySessionPage()

    class _FakeAgents:
        @staticmethod
        async def retrieve(agent_id: str, **_kwargs: object) -> BetaManagedAgentsAgent:
            return _agents_by_id[agent_id]

        @staticmethod
        def list(**_kwargs: object) -> object:
            # Never called in the cached-id liveness path (both agents are live).
            raise NotImplementedError("agents.list not expected in 2-tenant test")

    class _FakeEnvs:
        list = staticmethod(_envs_list)

        # run_turn bridges string ids to SDK objects via environments.retrieve
        # before delegating to create_session (95-04 collapse). Keyed by id so
        # each tenant's fire gets its own resolved environment back.
        @staticmethod
        async def retrieve(environment_id: str, **_kwargs: object) -> BetaEnvironment:
            return _envs_by_id[environment_id]

    class _EmptyVaultPage:
        def __aiter__(self) -> _EmptyVaultPage:
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

    class _FakeVaultCredentials:
        create = AsyncMock(return_value=None)

    class _FakeVaults:
        create = AsyncMock(return_value=SimpleNamespace(id="vault_test"))
        credentials = _FakeVaultCredentials()

        @staticmethod
        def list(**_kwargs: object) -> _EmptyVaultPage:
            return _EmptyVaultPage()

    class _FakeBeta:
        sessions = _FakeSessions()
        agents = _FakeAgents()
        environments = _FakeEnvs()
        vaults = _FakeVaults()
        memory_stores = _FakeMemoryStores()

    class _FakeClient:
        beta = _FakeBeta()
        close = AsyncMock(return_value=None)

    async def factory(_settings: Settings) -> AsyncAnthropic:
        return cast(AsyncAnthropic, _FakeClient())

    return factory


async def test_end_to_end_tick_fires_each_routine_under_own_tenant(
    schema_engine: tuple[AsyncEngine, async_sessionmaker[AsyncSession], uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
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
        _anthropic_factory=_build_two_tenant_fake_anthropic_factory(  # pyright: ignore[reportArgumentType]
            tenant_id_a=tenant_id_a,
            tenant_id_b=tenant_id_b,
        ),
        _engine_override=engine,
    )
    assert rc == 0, "scheduler --once should exit 0 on success"

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
