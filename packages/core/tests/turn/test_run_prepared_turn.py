"""Unit tests for daimon.core.turn.run.run_prepared_turn -- the D-08/D-09/D-10
one-shot dead-session recovery cycle: `run_turn` wired to the `PreparedTurn`
recorder, and on an upstream 404 with a live mapping row, mark-dead + recreate
+ rebind + reseed + one re-run, never looping on a second consecutive 404.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
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
from anthropic.types.beta.sessions.beta_managed_agents_agent_message_event import (
    BetaManagedAgentsAgentMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import BetaManagedAgentsTextBlock
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from daimon.core._models import ThreadSession
from daimon.core.config import McpSettings
from daimon.core.errors import TurnError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import usage_events
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.thread_sessions import (
    get_live_thread_session,
    get_thread_session_by_id,
)
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.prepare import ContinuityOutcome, PreparedTurn, bind_recorder
from daimon.core.turn.run import RunOutcome, _is_dead_session, run_prepared_turn
from daimon.core.turn.state import TurnState
from daimon.testing.db import build_test_engine
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
    make_fake_memory_store_handler,
    not_found_response,
    send_events_response,
    sse_response,
)
from daimon.testing.ma_models import ma_agent, ma_environment, ma_model_usage
from daimon.testing.turn_fakes import RecordingLifecycle
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from .conftest import make_status_idle

from daimon.testing.factories import (  # isort: skip
    make_account,
    make_tenant,
    make_thread_session,
)

_NOW = datetime(2026, 7, 28, tzinfo=UTC)


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
        tenant_id=tenant_id,
        external_user_id=external_user_id,
        ma_session_id=ma_session_id,
        model_id=admission.agent.model.id,
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
    sent_batches: list[tuple[str, list[dict[str, object]]]] | None = None,
    archived_session_ids: set[str] | None = None,
    events_by_session: dict[str, list[dict[str, object]]] | None = None,
    created_model: str = "claude-sonnet-4-6",
) -> MARouter:
    """A router serving memory-store cold-provision, session-create (each
    call assigns the next `sess_N` id), events.send, events.list and
    events.stream -- returning a 404 not_found for any session id in
    `dead_session_ids`, else one terminal `session.status_idle` (end_turn)
    event.

    `archived_session_ids` is the OTHER dead-session signature: the stream
    opens, and `events.send` answers with MA's archived-session 400. That is
    the signature whose event log is still readable (capability matrix
    P9.d/P9.c), which `events_by_session` supplies -- a session absent from
    that mapping 404s on `events.list`, exactly as a deleted one does.

    `created_model` is the model every session created through this router
    freezes; it decides whether the replacement can be sent a
    `system.message` at all."""
    archived = archived_session_ids or set()
    listable_events = events_by_session or {}
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
                    "model": {"id": created_model},
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

    def _send(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        sid = match.group("sid")
        if sid in archived:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"Cannot send events to archived session: {sid}",
                    },
                },
            )
        if sent_batches is not None:
            sent_batches.append((sid, json.loads(request.content)["events"]))
        return send_events_response()

    router.add("POST", r"/v1/sessions/(?P<sid>[^/]+)/events", _send)

    def _events_list(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        sid = match.group("sid")
        if sid not in listable_events:
            return not_found_response("session gone")
        return list_response(listable_events[sid])

    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events", _events_list)

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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
                model_usage=ma_model_usage(input_tokens=10, output_tokens=20),
                processed_at=datetime.now(UTC),
                type="span.model_request_end",
            )
            idle = make_status_idle(event_id="evt_idle")
            return sse_response([usage_evt.model_dump(mode="json"), idle.model_dump(mode="json")])

        router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events/stream", _stream)
        return router

    router = _router_with_usage_event({"sess_old"})
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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


async def test_recovery_rebinds_the_recorder_to_the_recreated_sessions_model(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The replacement session freezes its own agent snapshot, which need not
    be the model the dead session ran — so the rebound recorder must bill what
    `sessions.create` returned, not what the admission asked for."""
    from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
        BetaManagedAgentsSpanModelRequestEndEvent,
    )
    from daimon.core.stores import tenant_ledger

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-recovery-model",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = MARouter()
    memory_handler = make_fake_memory_store_handler()

    def _memory(request: httpx.Request, _match: object) -> httpx.Response:
        return memory_handler(request)

    router.add("POST", r"/v1/memory_stores", _memory)

    def _session_create(request: httpx.Request, _match: object) -> httpx.Response:
        body = json.loads(request.content)
        session_bodies.append(body)
        # MA reports the model the NEW session froze — opus, while the
        # admission below is still carrying sonnet.
        return httpx.Response(
            200,
            json={
                "id": "sess_new",
                "type": "session",
                "agent": {
                    "id": body["agent"],
                    "mcp_servers": [],
                    "model": {"id": "claude-opus-5"},
                    "name": "daimon",
                    "skills": [],
                    "tools": [],
                    "type": "agent",
                    "version": 2,
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
        if match.group("sid") == "sess_old":
            return not_found_response("session gone")
        usage_evt = BetaManagedAgentsSpanModelRequestEndEvent(
            id="evt_span",
            is_error=False,
            model_request_start_id="start_1",
            model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
            processed_at=datetime.now(UTC),
            type="span.model_request_end",
        )
        idle = make_status_idle(event_id="evt_idle")
        return sse_response([usage_evt.model_dump(mode="json"), idle.model_dump(mode="json")])

    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events/stream", _stream)

    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)
    assert admission.agent.model.id == "claude-sonnet-4-6", (
        "the admission must differ from the recreated session's model for this test to bite"
    )
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
        thread_id="thread-recovery-model",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True, "the dead session must be recovered exactly once"
    async with db_session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
        entries = await tenant_ledger.list_for_tenant(s, tenant_id=tenant.id)
    assert [usage.model for usage in rows] == ["claude-opus-5"], (
        "the rebound recorder must bill the recreated session's model, not the admission's"
    )
    assert [entry.delta_usd for entry in entries if entry.reason == "turn_debit"] == [
        Decimal("-5.000000")
    ], "1M input tokens must be debited at the recreated session's opus rate"

    async with db_session_factory() as s:
        live = await get_live_thread_session(
            s,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-recovery-model",
            account_id=account.id,
        )
    assert live is not None, "recovery must leave a live mapping row"
    assert live.effective_config is not None, "the replacement row must record its configuration"
    assert live.effective_config.model_id == "claude-opus-5", (
        "the replacement row's snapshot must be the model the new session froze"
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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

    def _events_gone(_request: httpx.Request, _match: object) -> httpx.Response:
        return not_found_response("session gone")  # deleted: its log went with it

    router.add("GET", r"/v1/sessions/[^/]+/events", _events_gone)

    def _stream(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        sid = match.group("sid")
        stream_calls.append(sid)
        return not_found_response("session gone")  # every session id is dead

    router.add("GET", r"/v1/sessions/(?P<sid>[^/]+)/events/stream", _stream)

    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
        system_blocks: object = (),
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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
        system_blocks: object = (),
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
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
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


# ---------------------------------------------------------------------------
# A replacement session's first turn: the framing it cannot answer without
# ---------------------------------------------------------------------------


def _replacement_continuity() -> ContinuityOutcome:
    """What the bind hands over after it replaced this caller's session."""
    return ContinuityOutcome(
        state="replaced",
        transfer_kind="full",
        user_prefix='<previous_session from="analysis-bot" trust="untrusted">\n'
        '<turn role="user">fit the hierarchical model</turn>\n</previous_session>',
        system_blocks=(
            {"type": "text", "text": "This conversation continues work started elsewhere."},
        ),
    )


async def test_a_replacement_sends_its_framing_with_the_first_user_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The successor has no conversation and no files of its own, so the first
    send carries both channels: the quoted prior conversation in front of the
    user message, and daimon's own words as a trailing system.message."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-framing",
        ma_session_id="sess_1",
    )
    await db_session.commit()

    sent: list[tuple[str, list[dict[str, object]]]] = []
    router = _router(session_bodies=[], dead_session_ids=set(), sent_batches=sent)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    admission = _admission(
        account_id=account.id,
        agent=ma_agent(id="ag_1", tenant_id=tenant.id),
        env=ma_environment(id="env_1", tenant_id=tenant.id),
    )
    continuity = _replacement_continuity()
    prepared = dataclasses.replace(
        _prepared_turn(
            deps=deps,
            admission=admission,
            tenant_id=tenant.id,
            external_user_id="user-1",
            ma_session_id="sess_1",
            mapping_id=row.id,
            session_account_id=account.id,
        ),
        continuity=continuity,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-framing",
        external_user_id="user-1",
        user_message="carry on please",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.state.error is None
    assert len(sent) == 1, "one turn, one send"
    session_id, batch = sent[0]
    assert session_id == "sess_1"
    assert [event["type"] for event in batch] == ["user.message", "system.message"], (
        "the API takes at most one system.message and it must come last"
    )
    text = "".join(
        block["text"]
        for block in batch[0]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert text == f"{continuity.user_prefix}\ncarry on please", (
        "the prior conversation goes in front of the message the person actually sent"
    )
    assert batch[1]["content"] == list(continuity.system_blocks), (
        "daimon's own framing rides the privileged channel verbatim"
    )
    assert outcome.continuity == continuity, "the adapter is told what the bind decided"


async def test_an_ordinary_turn_sends_exactly_the_user_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The default continuity is every turn that changed nothing. Those must
    send byte-identical bytes to before continuity existed."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-plain",
        ma_session_id="sess_1",
    )
    await db_session.commit()

    sent: list[tuple[str, list[dict[str, object]]]] = []
    router = _router(session_bodies=[], dead_session_ids=set(), sent_batches=sent)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(
            account_id=account.id,
            agent=ma_agent(id="ag_1", tenant_id=tenant.id),
            env=ma_environment(id="env_1", tenant_id=tenant.id),
        ),
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
        thread_id="thread-plain",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    _, batch = sent[0]
    assert [event["type"] for event in batch] == ["user.message"], (
        "no framing means no system.message"
    )
    text = "".join(
        block["text"]
        for block in batch[0]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert text == "hello", "an ordinary turn sends the message and nothing else"
    assert outcome.continuity.state == "continued"


async def test_recovery_reports_a_replacement_after_loss_and_keeps_the_framing_prefix(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the bound session is gone, the successor the recovery cycle builds
    mounts nothing: the workspace was lost, not handed over. The state says so
    (so the copy stays honest), while the quoted conversation -- the one thing
    that still crosses -- stays in front of the reseeded message."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-loss",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    sent: list[tuple[str, list[dict[str, object]]]] = []
    router = _router(session_bodies=[], dead_session_ids={"sess_old"}, sent_batches=sent)
    deps = _deps(sessionmaker=db_session_factory, router=router)
    continuity = _replacement_continuity()
    prepared = dataclasses.replace(
        _prepared_turn(
            deps=deps,
            admission=_admission(
                account_id=account.id,
                agent=ma_agent(id="ag_1", tenant_id=tenant.id),
                env=ma_environment(id="env_1", tenant_id=tenant.id),
            ),
            tenant_id=tenant.id,
            external_user_id="user-1",
            ma_session_id="sess_old",
            mapping_id=row.id,
            session_account_id=account.id,
        ),
        continuity=continuity,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-loss",
        external_user_id="user-1",
        user_message="carry on please",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True
    assert outcome.continuity.state == "replaced_after_loss", (
        "the session was lost, so the copy must not claim a planned replacement"
    )
    assert outcome.continuity.transfer_kind == "history", (
        "a deleted session's log is gone too (P9.e), so nothing but the thread crossed"
    )
    assert continuity.user_prefix not in outcome.continuity.user_prefix, (
        "the bind's framing described the workspace that just died; it is not restated"
    )

    recovery_session, recovery_batch = sent[-1]
    assert recovery_session == outcome.ma_session_id
    assert [event["type"] for event in recovery_batch] == ["user.message"], (
        "sonnet-4-6 rejects a system.message, so the framing travels in the message"
    )
    text = "".join(
        block["text"]
        for block in recovery_batch[0]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert text == f"{outcome.continuity.user_prefix}\nfull history reseed", (
        "the loss framing leads the reseeded message"
    )
    assert text.startswith("The workspace this conversation was running in was lost"), (
        "and it is this loss's framing, not the overtaken bind's"
    )
    assert "<previous_session" not in text, "there was no readable log to quote"


def _archived_session_log() -> list[dict[str, object]]:
    """The lost session's event log, still listable because MA archived it
    rather than deleting it (capability matrix P9.d)."""
    return [
        BetaManagedAgentsUserMessageEvent(
            id="sevt_user_1",
            type="user.message",
            content=[BetaManagedAgentsTextBlock(type="text", text="remember MARKER-K7VD22")],
            processed_at=None,
        ).model_dump(mode="json"),
        BetaManagedAgentsAgentMessageEvent(
            id="sevt_agent_1",
            type="agent.message",
            content=[BetaManagedAgentsTextBlock(type="text", text="Noted: MARKER-K7VD22")],
            processed_at=_NOW,
        ).model_dump(mode="json"),
    ]


async def _recover_from_archived_session(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: str,
    created_model: str,
    events_by_session: dict[str, list[dict[str, object]]],
) -> tuple[RunOutcome, list[tuple[str, list[dict[str, object]]]], ThreadSessionRow]:
    """Drive one recovery whose dead-session signal is MA's archived-400."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id=thread_id,
        ma_session_id="sess_archived",
    )
    await db_session.commit()

    sent: list[tuple[str, list[dict[str, object]]]] = []
    router = _router(
        session_bodies=[],
        dead_session_ids=set(),
        sent_batches=sent,
        archived_session_ids={"sess_archived"},
        events_by_session=events_by_session,
        created_model=created_model,
    )
    deps = _deps(sessionmaker=db_session_factory, router=router)
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(
            account_id=account.id,
            agent=ma_agent(id="ag_1", tenant_id=tenant.id),
            env=ma_environment(id="env_1", tenant_id=tenant.id),
        ),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_archived",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id=thread_id,
        external_user_id="user-1",
        user_message="what was the marker?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )
    return outcome, sent, row


async def test_recovery_quotes_the_archived_sessions_transcript_to_the_replacement(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Observed on staging: a session archived out from under a live thread
    healed silently, and the replacement was handed the platform history and
    nothing else -- it answered correctly only because the marker happened to
    be visible in the Discord thread. An archived session's event log is still
    listable, so the conversation can cross, quoted and untrusted, and the
    replacement can be told what it lost."""
    outcome, sent, _row = await _recover_from_archived_session(
        db_session,
        db_session_factory,
        thread_id="thread-archived-transcript",
        created_model="claude-sonnet-5",
        events_by_session={"sess_archived": _archived_session_log()},
    )

    assert outcome.recovered is True
    assert outcome.continuity.state == "replaced_after_loss", (
        "the workspace was lost, not handed over"
    )
    assert outcome.continuity.transfer_kind == "transcript", (
        "the conversation crossed and the files did not -- the middle rung, not the bottom one"
    )

    _, recovery_batch = sent[-1]
    assert [event["type"] for event in recovery_batch] == ["user.message", "system.message"], (
        "daimon's own words about the loss ride the privileged channel on a model that takes one"
    )
    text = "".join(
        block["text"]
        for block in recovery_batch[0]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert "<previous_session" in text, "the quoted conversation leads the reseeded message"
    assert "MARKER-K7VD22" in text, "including the exchange the person is about to ask about"
    assert text.endswith("\nfull history reseed"), "and the reseeded message follows it"

    system_text = "".join(
        block["text"]
        for block in recovery_batch[1]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert "was lost" in system_text, "the replacement is told what happened"
    assert "say plainly what is missing" in system_text, "and told to say so before continuing"
    assert "MARKER-K7VD22" not in system_text, (
        "quoted material never reaches the privileged channel"
    )


async def test_recovery_puts_the_loss_framing_in_the_message_without_system_support(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """haiku rejects a request carrying a `system.message`, so the framing has
    to degrade onto the ordinary channel rather than fail the recovered turn."""
    outcome, sent, _row = await _recover_from_archived_session(
        db_session,
        db_session_factory,
        thread_id="thread-archived-haiku",
        created_model="claude-haiku-4-5",
        events_by_session={"sess_archived": _archived_session_log()},
    )

    assert outcome.continuity.transfer_kind == "transcript"
    _, recovery_batch = sent[-1]
    assert [event["type"] for event in recovery_batch] == ["user.message"], (
        "a system.message would 400 the whole recovered turn on this model"
    )
    text = "".join(
        block["text"]
        for block in recovery_batch[0]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert text.startswith("The workspace this conversation was running in was lost"), (
        "the framing still reaches the model, just on the ordinary channel"
    )
    assert "<previous_session" in text, "with the quoted conversation after it"


async def test_recovery_falls_back_to_history_when_the_log_cannot_be_read(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bottom rung: nothing readable is left of the old session, so the
    platform-history reseed is all the replacement gets -- and the copy says
    `history`, never `transcript`."""
    outcome, sent, _row = await _recover_from_archived_session(
        db_session,
        db_session_factory,
        thread_id="thread-archived-gone",
        created_model="claude-sonnet-5",
        events_by_session={},
    )

    assert outcome.recovered is True
    assert outcome.continuity.transfer_kind == "history", (
        "an unreadable log is a worse gap than a quoted one and must not be described as one"
    )
    _, recovery_batch = sent[-1]
    text = "".join(
        block["text"]
        for block in recovery_batch[0]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert text == "full history reseed", (
        "with a system.message available, the reseeded message carries no prefix at all"
    )
    system_text = "".join(
        block["text"]
        for block in recovery_batch[1]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert "The previous session's log could not be read either" in system_text


async def test_recovery_records_the_dead_session_as_the_replacements_predecessor(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Observed on staging: after an unexpected loss the two rows for the
    thread sat side by side with no link between them — `predecessor_id=None`,
    `replaced_by_id=None`, `transfer_kind=None` — so nothing downstream could
    tell the successor apart from a first session, or say how much of the old
    one reached it. A loss is a replacement too, and the chain must say so."""
    outcome, _sent, dead_row = await _recover_from_archived_session(
        db_session,
        db_session_factory,
        thread_id="thread-loss-lineage",
        created_model="claude-sonnet-5",
        events_by_session={"sess_archived": _archived_session_log()},
    )

    assert outcome.recovered is True
    assert outcome.mapping_id is not None

    async with db_session_factory() as s:
        successor = await get_thread_session_by_id(s, id=outcome.mapping_id)
        dead = await get_thread_session_by_id(s, id=dead_row.id)

    assert successor is not None
    assert successor.predecessor_id == dead_row.id, (
        "the successor must point back at the session it was created to replace"
    )
    assert successor.transfer_kind == "transcript", (
        "the row records the rung the successor actually came in on"
    )
    assert successor.transfer_file_id is None, "no bundle crosses an unexpected loss"
    assert dead is not None
    assert dead.replaced_by_id == outcome.mapping_id, "and the chain closes from the other end"
    assert dead.status == "dead", (
        "the old row still says the session was lost, not deliberately superseded"
    )


async def test_recovery_records_the_history_rung_when_the_old_log_is_unreadable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The successor's row must not claim a transcript crossed when none did."""
    outcome, _sent, dead_row = await _recover_from_archived_session(
        db_session,
        db_session_factory,
        thread_id="thread-loss-lineage-history",
        created_model="claude-sonnet-5",
        events_by_session={},
    )

    assert outcome.mapping_id is not None
    async with db_session_factory() as s:
        successor = await get_thread_session_by_id(s, id=outcome.mapping_id)

    assert successor is not None
    assert successor.predecessor_id == dead_row.id
    assert successor.transfer_kind == "history", (
        "an unreadable log is the bottom rung and the row must say so"
    )


async def test_two_turns_recovering_one_dead_session_leave_one_live_row(
    db_session: AsyncSession,
    db_schema: str,
) -> None:
    """Two turns bound to one mapping (a Discord wizard submit does not queue
    behind a mention) both see its session die and both recover. Each used to
    mark the row dead and create its own replacement outside the bind lock,
    leaving two live rows for one (tenant, platform, thread, account): one MA
    session orphaned, and reads silently picking the newer one. Separate
    engines, because the per-thread advisory lock is connection-scoped and the
    shared-connection fixture would hide it."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-double-recovery",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids={"sess_old"})
    both_failed = asyncio.Event()
    dead_streams = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal dead_streams
        if request.method == "GET" and request.url.path == "/v1/sessions/sess_old/events/stream":
            # Both first attempts fail together, so both recoveries overlap.
            dead_streams += 1
            if dead_streams == 2:
                both_failed.set()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(both_failed.wait(), timeout=5)
        if request.method == "POST" and request.url.path == "/v1/sessions":
            # A slow sessions.create: the window a second recovery lands in.
            await asyncio.sleep(0.2)
        return router.dispatch(request)

    url = os.environ["DAIMON_DATABASE__TEST_URL"]
    engines = [build_test_engine(url, db_schema, poolclass=NullPool) for _ in range(2)]
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    admission = _admission(account_id=account.id, agent=agent, env=env)

    async def one_turn(engine: AsyncEngine, user: str) -> RunOutcome:
        deps = dataclasses.replace(
            _deps(
                sessionmaker=async_sessionmaker(bind=engine, expire_on_commit=False), router=router
            ),
            anthropic=anthropic.AsyncAnthropic(
                api_key="test",
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com"
                ),
                max_retries=0,
            ),
        )
        prepared = _prepared_turn(
            deps=deps,
            admission=admission,
            tenant_id=tenant.id,
            external_user_id=user,
            ma_session_id="sess_old",
            mapping_id=row.id,
            session_account_id=account.id,
        )
        return await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-double-recovery",
            external_user_id=user,
            user_message=f"hello from {user}",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
        )

    try:
        mention, submit = await asyncio.gather(
            one_turn(engines[0], "user-mention"), one_turn(engines[1], "user-submit")
        )
        async with async_sessionmaker(bind=engines[0])() as s:
            live_rows = (
                (
                    await s.execute(
                        select(ThreadSession).where(
                            ThreadSession.tenant_id == tenant.id,
                            ThreadSession.thread_id == "thread-double-recovery",
                            ThreadSession.status == "live",
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        for engine in engines:
            await engine.dispose()

    assert mention.recovered and submit.recovered, "both turns healed onto a live session"
    assert len(live_rows) == 1, (
        f"one thread must keep one live session row; got {len(live_rows)} "
        f"({[r.ma_session_id for r in live_rows]})"
    )
    assert len(session_bodies) == 1, "the second recovery adopts the first one's replacement"
    assert mention.mapping_id == submit.mapping_id == live_rows[0].id, (
        "both turns report the one live replacement"
    )
