"""Unit tests for daimon.core.turn.run.run_prepared_turn -- the D-08/D-09/D-10
one-shot dead-session recovery cycle: `run_turn` wired to the `PreparedTurn`
recorder, and on an upstream 404 with a live mapping row, mark-dead + recreate
+ rebind + reseed + one re-run, never looping on a second consecutive 404.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import anthropic
import httpx
import pytest
from anthropic.types import RawMessageStreamEvent
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.errors import TurnError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import usage_events
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.prepare import PreparedTurn, bind_recorder
from daimon.core.turn.run import _is_dead_session, run_prepared_turn
from daimon.core.turn.state import TurnState
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    build_fake_anthropic,
    make_fake_memory_store_handler,
    not_found_response,
    send_events_response,
    sse_response,
)
from daimon.testing.turn_fakes import RecordingLifecycle
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import make_status_idle

from daimon.testing.factories import (  # isort: skip
    make_account,
    make_tenant,
    make_thread_session,
)

_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _agent(*, agent_id: str, tenant_id: uuid.UUID, name: str = "daimon") -> BetaManagedAgentsAgent:
    now = datetime.now(UTC)
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        description=None,
        metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: name},
        mcp_servers=[],
        tools=[],
        skills=[],
        created_at=now,
        updated_at=now,
        archived_at=None,
    )


def _env(*, env_id: str, tenant_id: uuid.UUID, name: str = "default") -> BetaEnvironment:
    now_iso = datetime.now(UTC).isoformat()
    return BetaEnvironment(
        id=env_id,
        type="environment",
        name=name,
        description="",
        config=EMPTY_CLOUD_CONFIG,
        metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: name},
        created_at=now_iso,
        updated_at=now_iso,
        archived_at=None,
    )


def _admission(
    *, account_id: uuid.UUID, agent: BetaManagedAgentsAgent, env: BetaEnvironment
) -> Admission:
    return Admission(
        account_id=account_id,
        agent=agent,
        environment=env,
        config=ResolvedConfig(agent_name="daimon", environment_name="default"),
    )


def _deps(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    router: MARouter,
) -> TurnDeps:
    return TurnDeps(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/nonexistent"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1.0"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )


def _prepared_turn(
    *,
    deps: TurnDeps,
    admission: Admission,
    tenant_id: uuid.UUID,
    external_user_id: str,
    ma_session_id: str,
    mapping_id: uuid.UUID | None,
    session_account_id: uuid.UUID,
) -> PreparedTurn:
    """Build a PreparedTurn directly (bypassing bind_session) so tests can
    control ma_session_id/mapping_id explicitly -- including the
    mapping_id=None negative case bind_session never actually produces."""
    record = bind_recorder(
        deps,
        admission,
        tenant_id=tenant_id,
        external_user_id=external_user_id,
        ma_session_id=ma_session_id,
    )
    return PreparedTurn(
        admission=admission,
        ma_session_id=ma_session_id,
        mapping_id=mapping_id,
        watermark=None,
        reused=True,
        session_account_id=session_account_id,
        _record=record,
    )


def _router(
    *,
    session_bodies: list[dict[str, object]],
    dead_session_ids: set[str],
) -> MARouter:
    """A router serving memory-store cold-provision, session-create (each
    call assigns the next `sess_N` id), events.send, and events.stream --
    returning a 404 not_found for any session id in `dead_session_ids`, else
    one terminal `session.status_idle` (end_turn) event."""
    router = MARouter()
    memory_handler = make_fake_memory_store_handler()

    def _memory(request: httpx.Request, _match: object) -> httpx.Response:
        return memory_handler(request)

    router.add("POST", r"/v1/memory_stores", _memory)

    def _session_create(request: httpx.Request, _match: object) -> httpx.Response:
        body = json.loads(request.content)
        new_id = f"sess_{len(session_bodies) + 1}"
        session_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "id": new_id,
                "type": "session",
                "agent": {
                    "id": body["agent"],
                    "mcp_servers": [],
                    "model": {"id": "claude-sonnet-4-6"},
                    "name": "daimon",
                    "skills": [],
                    "tools": [],
                    "type": "agent",
                    "version": 1,
                },
                "created_at": "2026-07-28T00:00:00Z",
                "outcome_evaluations": [],
                "environment_id": body["environment_id"],
                "metadata": {},
                "resources": [],
                "stats": {},
                "status": "idle",
                "updated_at": "2026-07-28T00:00:00Z",
                "usage": {},
                "vault_ids": [],
            },
        )

    router.add("POST", r"/v1/sessions", _session_create)

    def _send(_request: httpx.Request, _match: object) -> httpx.Response:
        return send_events_response()

    router.add("POST", r"/v1/sessions/[^/]+/events", _send)

    def _stream(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        sid = match.group("sid")
        if sid in dead_session_ids:
            return not_found_response("session gone")
        idle = make_status_idle(event_id="evt_idle")
        return sse_response([idle.model_dump(mode="json")])

    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events/stream", _stream)

    return router


async def _reseed() -> str:
    return "full history reseed"


def _recovery_lifecycle(_cancel: asyncio.Event) -> RecordingLifecycle:
    return RecordingLifecycle()


class _AlwaysRaisingRenderLifecycle:
    """`on_render` always raises; every other hook forwards to an inner
    `RecordingLifecycle` so terminal-hook firing is still observable.

    Used to prove the driver's per-tick render error policy composes
    cleanly with D-08 recovery: a render failure inside the recovery
    turn's own render loop must not derail `run_prepared_turn`'s outcome
    -- `on_render`'s exceptions are swallowed inside `run_turn`'s render
    loop itself, long before `run_prepared_turn`'s own error handling
    could ever see them.
    """

    def __init__(self) -> None:
        self.inner = RecordingLifecycle()
        self.render_calls = 0

    async def on_render(self, state: TurnState) -> None:
        self.render_calls += 1
        raise RuntimeError("adapter render boom")

    async def on_terminal_success(self, state: TurnState) -> None:
        await self.inner.on_terminal_success(state)

    async def on_terminal_failure(self, state: TurnState, err: Exception) -> None:
        await self.inner.on_terminal_failure(state, err)

    async def on_sse_event(self, event: RawMessageStreamEvent) -> None:
        await self.inner.on_sse_event(event)

    async def on_reconnect(self, reason: ReconnectReason) -> None:
        await self.inner.on_reconnect(reason)

    async def on_rate_limited(self, until: datetime | None) -> None:
        await self.inner.on_rate_limited(until)

    async def on_interrupt_sent(self, source: InterruptSource) -> None:
        await self.inner.on_interrupt_sent(source)


async def test_happy_path_runs_once_and_returns_recovered_false(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-1",
        ma_session_id="sess_1",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids=set())
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_1",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is False, "a clean run must not recover"
    assert outcome.ma_session_id == "sess_1", "must report the original session id"
    assert outcome.mapping_id == row.id, "must report the original mapping id"
    assert outcome.state.error is None, "a clean idle-end-turn run has no error"
    assert len(session_bodies) == 0, "no create_session call on the happy path"


async def test_dead_session_recovers_once_and_rebinds_recorder(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-2",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids={"sess_old"})
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-2",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True, "a dead-session signature must trigger exactly one recovery"
    assert outcome.ma_session_id != "sess_old", "the final session id must be the new one"
    assert outcome.mapping_id != row.id, "the final mapping id must be the new row"
    assert outcome.state.error is None, "the recovered re-run completes cleanly"
    assert len(session_bodies) == 1, "recovery must call create_session exactly once"

    async with db_session_factory() as s:
        live = await get_live_thread_session(
            s,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-2",
            account_id=account.id,
        )
    assert live is not None, "the new mapping row must be live"
    assert live.id == outcome.mapping_id, "get_live_thread_session must return the new row"
    assert live.ma_session_id == outcome.ma_session_id, (
        "the live row's session id must be the new one"
    )

    async with db_session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
    # The happy-path idle event has no span.model_request_end, so the recorder
    # is never invoked by run_turn itself -- assert the REBOUND recorder,
    # once invoked directly, writes against the NEW session id (not the old).
    assert len(rows) == 0, "no span.model_request_end event fired during either run"


async def test_render_failure_during_recovery_does_not_prevent_recovery(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Plan 19-06's per-tick render error policy composes cleanly with D-08
    recovery. Note on scoping: the doomed FIRST attempt in this scenario
    404s directly at stream-open (see `_router`'s `dead_session_ids`
    handling), so it never calls `on_render` at all -- there is nothing
    for `_DeferredFailureLifecycle` to hold back. The render failure this
    test exercises is inside the RECOVERY turn's own render loop instead,
    reached via the `recovery_lifecycle` factory `run_prepared_turn` calls
    once recovery starts.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-render-fail",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids={"sess_old"})
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    recovery_lc = _AlwaysRaisingRenderLifecycle()

    def _raising_recovery_lifecycle(_cancel: asyncio.Event) -> TurnLifecycle:
        return recovery_lc

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-render-fail",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_raising_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True, (
        "a render failure inside the recovery turn's own render loop must "
        "not prevent run_prepared_turn from reporting a successful recovery"
    )
    assert outcome.state.error is None, "the recovered re-run completes cleanly"
    assert recovery_lc.render_calls >= 1, "on_render must have been attempted (and failed)"
    assert len(recovery_lc.inner.terminal_success) == 1, (
        "terminal hooks must still fire despite every render attempt failing"
    )


async def test_dead_session_recorder_rebind_targets_new_session_id(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SPEC Req 4 acceptance: a recovered turn's usage_events.managed_session_id
    equals the NEW session id, proven by driving a real span.model_request_end
    event through the recovered run and reading the row back."""
    from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
        BetaManagedAgentsSpanModelRequestEndEvent,
    )
    from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
        BetaManagedAgentsSpanModelUsage,
    )

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-3",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []

    def _router_with_usage_event(dead_ids: set[str]) -> MARouter:
        router = MARouter()
        memory_handler = make_fake_memory_store_handler()

        def _memory(request: httpx.Request, _match: object) -> httpx.Response:
            return memory_handler(request)

        router.add("POST", r"/v1/memory_stores", _memory)

        def _session_create(request: httpx.Request, _match: object) -> httpx.Response:
            body = json.loads(request.content)
            new_id = f"sess_{len(session_bodies) + 1}"
            session_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": new_id,
                    "type": "session",
                    "agent": {
                        "id": body["agent"],
                        "mcp_servers": [],
                        "model": {"id": "claude-sonnet-4-6"},
                        "name": "daimon",
                        "skills": [],
                        "tools": [],
                        "type": "agent",
                        "version": 1,
                    },
                    "created_at": "2026-07-28T00:00:00Z",
                    "outcome_evaluations": [],
                    "environment_id": body["environment_id"],
                    "metadata": {},
                    "resources": [],
                    "stats": {},
                    "status": "idle",
                    "updated_at": "2026-07-28T00:00:00Z",
                    "usage": {},
                    "vault_ids": [],
                },
            )

        router.add("POST", r"/v1/sessions", _session_create)

        def _send(_request: httpx.Request, _match: object) -> httpx.Response:
            return send_events_response()

        router.add("POST", r"/v1/sessions/[^/]+/events", _send)

        def _stream(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
            sid = match.group("sid")
            if sid in dead_ids:
                return not_found_response("session gone")
            usage_evt = BetaManagedAgentsSpanModelRequestEndEvent(
                id="evt_span",
                is_error=False,
                model_request_start_id="start_1",
                model_usage=BetaManagedAgentsSpanModelUsage(
                    input_tokens=10,
                    output_tokens=20,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
                processed_at=datetime.now(UTC),
                type="span.model_request_end",
            )
            idle = make_status_idle(event_id="evt_idle")
            return sse_response([usage_evt.model_dump(mode="json"), idle.model_dump(mode="json")])

        router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events/stream", _stream)
        return router

    router = _router_with_usage_event({"sess_old"})
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-3",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True
    async with db_session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
    assert len(rows) == 1, "the recovered run's span.model_request_end must record exactly one row"
    assert rows[0].managed_session_id == outcome.ma_session_id, (
        "the recovered turn's usage_events.managed_session_id must equal the NEW session id"
    )
    assert rows[0].managed_session_id != "sess_old", (
        "the recorded session id must not be the stale old session"
    )


async def test_dead_session_without_mapping_id_does_not_recover(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids={"sess_old"})
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=None,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-4",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is False, "a 404 with mapping_id=None must not trigger recovery"
    assert outcome.state.error is not None
    assert outcome.state.error.kind == "upstream"
    assert len(session_bodies) == 0, "no create_session call when mapping_id is None"


async def test_non_404_upstream_error_does_not_recover(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-5",
        ma_session_id="sess_bad",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = MARouter()

    def _400(_request: httpx.Request, _match: object) -> httpx.Response:
        return httpx.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad id"}},
        )

    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events/stream", _400)

    def _explode(_request: httpx.Request, _match: object) -> httpx.Response:
        raise AssertionError("create_session must not be called on a 400")

    router.add("POST", r"/v1/sessions", _explode)

    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_bad",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-5",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is False, "a 400 must not trigger recovery"
    assert outcome.state.error is not None
    assert outcome.state.error.kind == "upstream"
    assert len(session_bodies) == 0, "a 400 must never call create_session"


async def test_second_consecutive_dead_session_does_not_loop(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-6",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    stream_calls: list[str] = []
    router = MARouter()
    memory_handler = make_fake_memory_store_handler()

    def _memory(request: httpx.Request, _match: object) -> httpx.Response:
        return memory_handler(request)

    router.add("POST", r"/v1/memory_stores", _memory)

    def _session_create(request: httpx.Request, _match: object) -> httpx.Response:
        body = json.loads(request.content)
        new_id = f"sess_{len(session_bodies) + 1}"
        session_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "id": new_id,
                "type": "session",
                "agent": {
                    "id": body["agent"],
                    "mcp_servers": [],
                    "model": {"id": "claude-sonnet-4-6"},
                    "name": "daimon",
                    "skills": [],
                    "tools": [],
                    "type": "agent",
                    "version": 1,
                },
                "created_at": "2026-07-28T00:00:00Z",
                "outcome_evaluations": [],
                "environment_id": body["environment_id"],
                "metadata": {},
                "resources": [],
                "stats": {},
                "status": "idle",
                "updated_at": "2026-07-28T00:00:00Z",
                "usage": {},
                "vault_ids": [],
            },
        )

    router.add("POST", r"/v1/sessions", _session_create)

    def _send(_request: httpx.Request, _match: object) -> httpx.Response:
        return send_events_response()

    router.add("POST", r"/v1/sessions/[^/]+/events", _send)

    def _stream(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        sid = match.group("sid")
        stream_calls.append(sid)
        return not_found_response("session gone")  # every session id is dead

    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events/stream", _stream)

    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-6",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True, "one recovery attempt must fire on the first 404"
    assert outcome.state.error is not None, "the second TurnState (also a 404) is returned as-is"
    assert outcome.state.error.kind == "upstream"
    assert len(session_bodies) == 1, (
        "exactly one recovery create_session call -- no second recovery"
    )
    assert len(stream_calls) == 2, "exactly two run_turn attempts total -- no further retry loop"


def _api_status_error(status_code: int, message: str) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/sessions/sess_1/events")
    response = httpx.Response(status_code, request=request, json={"error": {"message": message}})
    return anthropic.APIStatusError(message, response=response, body=None)


def _state_with_upstream_cause(cause: Exception) -> TurnState:
    return TurnState(error=TurnError(kind="upstream", message=str(cause), cause=cause))


def test_dead_session_detects_archived_400_so_a_thread_can_heal() -> None:
    """A terminated session 400s rather than 404ing, and must still recover.

    Regression: staging session sesn_01TBcsjhyD4KMEc6wasC3vyg. MA terminated
    it, the mapping row kept pointing at it, and because only 404 counted as
    dead, every later message in that thread failed forever.
    """
    cause = _api_status_error(
        400, "Cannot send events to archived session: sesn_01TBcsjhyD4KMEc6wasC3vyg"
    )

    assert _is_dead_session(_state_with_upstream_cause(cause)) is True


def test_dead_session_still_detects_404() -> None:
    assert _is_dead_session(_state_with_upstream_cause(_api_status_error(404, "not found"))) is True


def test_dead_session_ignores_other_400s() -> None:
    """A malformed-id 400 must surface as a turn error, not trigger a recreate."""
    cause = _api_status_error(400, "session_id: invalid format")

    assert _is_dead_session(_state_with_upstream_cause(cause)) is False


def test_dead_session_ignores_a_terminating_turn_with_no_api_cause() -> None:
    """The turn that KILLS the session must not recover — it would replay the poison.

    Its error carries no APIStatusError; recovery happens on the next message,
    which is the first to see the archived-session 400.
    """
    state = TurnState(error=TurnError(kind="upstream", message="session terminated by MA"))

    assert _is_dead_session(state) is False


async def test_recovered_turn_never_shows_the_user_a_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A recoverable dead session must not paint a terminal failure.

    The adapter renders its red error embed from ``on_terminal_failure``, so
    delivering that hook for an error we heal three seconds later shows the
    user a scary upstream 400 that is then retracted.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-quiet-recovery",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    router = _router(session_bodies=[], dead_session_ids={"sess_old"})
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    caller_lifecycle = RecordingLifecycle()
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-quiet-recovery",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=caller_lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True, "the dead session must still recover"
    assert caller_lifecycle.terminal_failures == [], (
        "a recovered turn must never deliver on_terminal_failure -- that hook is "
        "what paints the error embed the user sees retracted"
    )


async def test_unrecovered_failure_is_still_delivered_to_the_caller(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Withholding is only for the recovery path; a real failure must surface."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-real-failure",
        ma_session_id="sess_bad",
    )
    await db_session.commit()

    router = MARouter()

    def _400(_request: httpx.Request, _match: object) -> httpx.Response:
        return httpx.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad id"}},
        )

    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events/stream", _400)

    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_bad",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    caller_lifecycle = RecordingLifecycle()
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-real-failure",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=caller_lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is False, "a plain 400 must not recover"
    assert len(caller_lifecycle.terminal_failures) == 1, (
        "a turn that is not recovered must still deliver its failure exactly once"
    )


async def test_ceiling_breach_on_first_attempt_returns_ceiling_error_and_marks_mapping_dead(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-ceiling-first",
        ma_session_id="sess_1",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids=set())
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_1",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    caller_lifecycle = RecordingLifecycle()
    past_deadline = datetime.now(UTC) - timedelta(seconds=5)
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-ceiling-first",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=caller_lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        deadline=past_deadline,
    )

    assert outcome.state.error is not None
    assert outcome.state.error.kind == "ceiling"
    assert outcome.recovered is False, "a first-attempt breach never entered recovery"
    assert outcome.mapping_id == row.id, "must report the originally prepared mapping id"
    assert outcome.ma_session_id == "sess_1"
    assert len(session_bodies) == 0, "a first-attempt breach must never call create_session"
    assert len(caller_lifecycle.terminal_failures) == 1, (
        "on_terminal_failure must be delivered exactly once on the caller's own lifecycle"
    )
    assert caller_lifecycle.terminal_failures[0][1].kind == "ceiling"  # type: ignore[attr-defined]

    async with db_session_factory() as s:
        live = await get_live_thread_session(
            s,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-ceiling-first",
            account_id=account.id,
        )
    assert live is None, "a ceiling breach must mark the mapping dead (no longer live)"


async def test_ceiling_breach_during_recovery_marks_the_new_mapping_dead_not_the_old_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """First attempt returns a dead-session 404, so recovery recreates a new
    session/mapping; the ceiling then breaches inside the recovery run_turn
    call itself. The NEW mapping must be marked dead, not the stale one the
    ordinary recovery cycle already marked dead on its own."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-ceiling-recovery",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids={"sess_old"})

    # The first attempt's 404 and the recreate are cheap in-process/DB calls;
    # the SECOND run_turn call's stream open is the one deliberately slowed
    # down (real asyncio.sleep, not a past deadline) so the ceiling breaches
    # specifically inside the recovery attempt rather than the first one.
    # create_fresh_session always allocates the router's next sequential id,
    # and this is the router's only create_session call in this test, so the
    # recreated session id is deterministically "sess_1".
    async def _slow_stream_for_recovered_session(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/sess_1/events/stream":
            await asyncio.sleep(1.0)
        return router.dispatch(request)

    transport = httpx.MockTransport(_slow_stream_for_recovered_session)
    http_client = httpx.AsyncClient(transport=transport, base_url="https://api.anthropic.com")
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        anthropic=anthropic.AsyncAnthropic(api_key="test", http_client=http_client),
    )
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    deadline = datetime.now(UTC) + timedelta(seconds=0.2)

    caller_lifecycle = RecordingLifecycle()
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-ceiling-recovery",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=caller_lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        deadline=deadline,
    )

    assert outcome.state.error is not None
    assert outcome.state.error.kind == "ceiling"
    assert len(session_bodies) == 1, "recovery must still have created exactly one new session"
    assert outcome.mapping_id != row.id, "the reported mapping id must be the NEW one"
    assert outcome.ma_session_id != "sess_old", "the reported session id must be the NEW one"

    async with db_session_factory() as s:
        old_live = await get_live_thread_session(
            s,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-ceiling-recovery",
            account_id=account.id,
        )
    assert old_live is None, (
        "the NEW mapping must be marked dead too (get_live_thread_session excludes both)"
    )


async def test_ceiling_breach_never_triggers_dead_session_recovery(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """D-10 / T-19-03-B pin: a ceiling breach must never be mistaken for a
    dead-session (404) signal, which would re-run a 45-minute turn as a fresh
    one -- create_fresh_session must be called zero times on a first-attempt
    ceiling breach."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-ceiling-no-loop",
        ma_session_id="sess_1",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids=set())
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_1",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    past_deadline = datetime.now(UTC) - timedelta(seconds=5)
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-ceiling-no-loop",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        deadline=past_deadline,
    )

    assert outcome.recovered is False
    assert len(session_bodies) == 0, "a ceiling error must never trigger create_fresh_session"


async def test_run_prepared_turn_default_deadline_none_still_succeeds_on_the_happy_path(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression pin: deadline=None must not change happy-path behavior."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-ceiling-default-none",
        ma_session_id="sess_1",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids=set())
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_1",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-ceiling-default-none",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is False
    assert outcome.state.error is None
    assert outcome.ma_session_id == "sess_1"
    assert outcome.mapping_id == row.id


# --- D-07: cancel coverage over the dead-session recovery cycle (19-05) ----
#
# Both tests below monkeypatch `daimon.core.turn.run.run_turn` itself rather
# than racing a real driver call against a real cancel signal. The driver's
# OWN cancel race (stream-open, send-initial, consume loop) is already
# pinned by test_driver_cancel.py -- what's under test here is
# run_prepared_turn's OWN orchestration: does it abort recovery when cancel
# is already set, and does the mirror task actually forward a late cancel
# into the recovery turn's own event. Faking `run_turn` isolates that from
# the driver's internal race timing, which would otherwise make the exact
# moment cancel becomes visible to the FIRST attempt's own stream-open race
# nondeterministic (see 19-05-PLAN.md's Task 1 for that race's mechanics).


def _leaked_turn_task_names() -> list[str]:
    return [
        t.get_name()
        for t in asyncio.all_tasks()
        if t.get_name().startswith("turn.") and not t.done()
    ]


async def test_cancel_set_before_recovery_starts_aborts_recovery_and_flushes_held_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-07(a): a cancel already signalled by the time the dead-session
    signature is observed must abort recovery -- no create_fresh_session
    call, no second run_turn -- and still deliver the withheld first-attempt
    failure to the caller's lifecycle exactly once."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-cancel-before-recovery",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids=set())
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    cancel = asyncio.Event()
    dead_session_cause = _api_status_error(404, "not found")
    call_count = 0

    async def _fake_run_turn(
        *,
        anthropic: object,
        session_id: str,
        user_message: str,
        lifecycle: TurnLifecycle,
        cancel: asyncio.Event,
        render_interval_s: object,
        billing: object,
        image_blocks: object,
    ) -> TurnState:
        nonlocal call_count
        call_count += 1
        err = TurnError(kind="upstream", message="not found", cause=dead_session_cause)
        state = TurnState(error=err)
        await lifecycle.on_terminal_failure(state, err)
        # Cancel arrives right as the first attempt observes the
        # dead-session signature -- strictly before run_prepared_turn's own
        # recovery-abort check runs.
        cancel.set()
        return state

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)

    caller_lifecycle = RecordingLifecycle()
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-cancel-before-recovery",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=caller_lifecycle,
        cancel=cancel,
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert call_count == 1, "recovery must never call run_turn a second time"
    assert outcome.recovered is False, (
        "an already-cancelled dead-session signature must not recover"
    )
    assert len(session_bodies) == 0, "no create_fresh_session call when cancel already fired"
    assert outcome.ma_session_id == "sess_old", "must report the original (unrecovered) session id"
    assert outcome.mapping_id == row.id, "must report the original (unrecovered) mapping id"
    assert outcome.state.error is not None
    assert outcome.state.error.kind == "upstream", (
        "the withheld dead-session failure is returned as-is"
    )
    assert len(caller_lifecycle.terminal_failures) == 1, (
        "the withheld first-attempt failure must still be delivered exactly once"
    )


async def test_cancel_during_recovery_mirrors_into_the_recovery_turn_and_interrupts_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-07(b): a cancel set on the ORIGINAL event while the recovery turn is
    in flight must still interrupt the recovery turn -- proving the
    `turn.cancel_mirror` task forwards it into `fresh_cancel`, the event the
    RECOVERY run_turn call actually owns (not the original `cancel`)."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-cancel-during-recovery",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids=set())
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    dead_session_cause = _api_status_error(404, "not found")
    call_count = 0

    async def _fake_run_turn(
        *,
        anthropic: object,
        session_id: str,
        user_message: str,
        lifecycle: TurnLifecycle,
        cancel: asyncio.Event,
        render_interval_s: object,
        billing: object,
        image_blocks: object,
    ) -> TurnState:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            err = TurnError(kind="upstream", message="not found", cause=dead_session_cause)
            state = TurnState(error=err)
            await lifecycle.on_terminal_failure(state, err)
            return state
        # Recovery attempt: this `cancel` kwarg IS `fresh_cancel` (the event
        # run_prepared_turn built for the recovery turn) -- block until it
        # fires, proving the mirror is what unblocks it, since the ORIGINAL
        # event is never passed to this call directly.
        await cancel.wait()
        return TurnState(error=TurnError(kind="interrupted", message="interrupted during recovery"))

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)

    original_cancel = asyncio.Event()

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.02)
        original_cancel.set()

    caller_lifecycle = RecordingLifecycle()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_cancel_soon())
        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-cancel-during-recovery",
            external_user_id="user-1",
            user_message="hello",
            lifecycle=caller_lifecycle,
            cancel=original_cancel,
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
        )

    assert call_count == 2, "the recovery must have actually run a second attempt"
    assert outcome.recovered is True
    assert outcome.state.error is not None
    assert outcome.state.error.kind == "interrupted", "the mirror must have forwarded the cancel"
    assert _leaked_turn_task_names() == [], (
        "no turn.cancel_mirror task may linger after the call returns"
    )


async def test_recovery_happy_path_unaffected_by_the_cancel_mirror_and_leaks_no_task(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Regression: an ordinary recovery (cancel never set) still recreates
    the session, rebinds the recorder, and returns recovered=True -- adding
    the mirror task must not change happy-path behavior or leak a task."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-recovery-happy-path-mirror",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids={"sess_old"})
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = _agent(agent_id="ag_1", tenant_id=tenant.id)
    env = _env(env_id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-recovery-happy-path-mirror",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True
    assert outcome.ma_session_id != "sess_old"
    assert outcome.state.error is None
    assert len(session_bodies) == 1
    assert _leaked_turn_task_names() == [], (
        "no turn.cancel_mirror task may linger after a clean recovery"
    )
