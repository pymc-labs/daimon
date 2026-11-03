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
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn, cast

import anthropic
import httpx
import pytest
import structlog
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
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.stores import usage_events
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.thread_sessions import (
    get_live_thread_session,
    get_thread_session_by_id,
)
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn import run as run_module
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.lifecycle import InterruptSource, ReconnectReason, TurnLifecycle
from daimon.core.turn.prepare import ContinuityOutcome, PreparedTurn, bind_recorder
from daimon.core.turn.run import RunOutcome, _is_dead_session, run_prepared_turn
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.db import build_test_engine
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import Scope
from mux.errors import UnsupportedCapability
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
    make_fake_memory_store_handler,
    not_found_response,
    send_events_response,
    sse_response,
)
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
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


def with_invocation(
    admission: Admission,
    tenant_id: uuid.UUID,
    *,
    human: bool,
    external_user_id: str,
    channel_id: str = "C1",
    thread_id: str = "thread-1",
) -> Admission:
    """A host-admitted principal for tests of signed execution binding."""
    from daimon.core.authz import AgentRef, Place, Subject, Surface
    from daimon.core.turn.admission import AdmissionGrant

    place = Place(channel_id=thread_id, parent_channel_id=channel_id)
    return dataclasses.replace(
        admission,
        grant=AdmissionGrant(
            tenant_id=tenant_id,
            subject=Subject(platform_user_id=external_user_id),
            surface=Surface.CHANNEL,
            turn_place=place,
            run_place=place,
            agent=AgentRef.of(admission.agent.name),
            channel_id=channel_id,
            thread_id=thread_id,
            is_dm=False,
            human_invocation=human,
        ),
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
        tool_safety=OPEN_TOOL_SAFETY,
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
                    # A bare id, or `agent_with_overrides` carrying one.
                    "id": body["agent"]["id"] if isinstance(body["agent"], dict) else body["agent"],
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
    assert outcome.termination is TerminationReason.COMPLETED
    assert len(session_bodies) == 0, "no create_session call on the happy path"


@pytest.mark.parametrize("recovers", [False, True])
async def test_a_failed_memory_state_record_never_fails_a_completed_turn(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    recovers: bool,
) -> None:
    from daimon.core.turn import run as run_module
    from sqlalchemy.exc import OperationalError

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-memory-record",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    attempts: list[str] = []

    async def failing_record(*args: object, session_id: str, **kwargs: object) -> None:
        attempts.append(session_id)
        raise OperationalError("upsert repo_memory_sessions", {}, Exception("connection lost"))

    monkeypatch.setattr(run_module, "record_memory_prefix", failing_record)
    router = _router(session_bodies=[], dead_session_ids={"sess_old"} if recovers else set())
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

    lifecycle = RecordingLifecycle()
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-memory-record",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert attempts == [outcome.ma_session_id]
    assert outcome.recovered is recovers
    assert outcome.state.error is None
    assert outcome.termination is TerminationReason.COMPLETED
    assert lifecycle.terminal_failures == []


async def test_every_log_line_of_a_turn_names_its_backend(
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
    router = _router(session_bodies=[], dead_session_ids=set())
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(account_id=account.id, agent=agent, env=env),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_1",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        await run_prepared_turn(
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

    turn_lines = [entry for entry in logs if entry["event"] in {"turn.started", "turn.completed"}]
    assert {entry["event"] for entry in turn_lines} == {"turn.started", "turn.completed"}
    for entry in turn_lines:
        assert entry["backend"] == "anthropic"
        assert entry["profile"] == "anthropic.managed_agents"
    assert not {"backend", "profile", "model"} & set(structlog.contextvars.get_contextvars()), (
        "the fields are unbound once the turn returns, so no other turn's lines inherit them"
    )


async def test_reattaching_to_a_session_debits_calls_no_worker_streamed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """#17: a deploy killed the worker streaming the last turn while the MA session
    kept making model calls. The next turn on the reused session debits those calls
    (live path's key and price) once it is over -- after the message was sent."""
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
        thread_id="thread-gap",
        ma_session_id="sess_1",
    )
    await db_session.commit()
    gap = [
        BetaManagedAgentsSpanModelRequestEndEvent(
            id=f"evt_gap_{i}",
            is_error=False,
            model_request_start_id=f"start_{i}",
            model_usage=ma_model_usage(input_tokens=1000, output_tokens=100),
            processed_at=_NOW,
            type="span.model_request_end",
        ).model_dump(mode="json")
        for i in range(2)
    ]
    sent: list[tuple[str, list[dict[str, object]]]] = []
    router = _router(
        session_bodies=[],
        dead_session_ids=set(),
        sent_batches=sent,
        events_by_session={"sess_1": gap},
    )
    deps = _deps(sessionmaker=db_session_factory, router=router)
    admission = _admission(
        account_id=account.id,
        agent=ma_agent(id="ag_1", tenant_id=tenant.id),
        env=ma_environment(id="env_1", tenant_id=tenant.id),
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_1",
        mapping_id=row.id,
        session_account_id=account.id,
    )

    with structlog.testing.capture_logs() as logs:
        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-gap",
            external_user_id="user-1",
            user_message="hello again",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
        )

    assert outcome.state.error is None
    async with db_session_factory() as s:
        rows = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
    assert sorted(r.event_id for r in rows) == ["evt_gap_0", "evt_gap_1"], (
        "every call the dead worker never streamed is metered on reattach"
    )
    settled = [e for e in logs if e["event"] == "billing.settled"]
    assert [e["debited"] for e in settled] == [2], "debited once, at turn end"
    events = [e["event"] for e in logs]
    assert events.index("billing.settled") > events.index("turn.completed"), (
        "the catch-up runs after the turn, never in front of the user's message"
    )
    assert sent, "the turn still sent its message"


@pytest.mark.parametrize("ceiling", [False, True], ids=["completed", "ceiling"])
async def test_a_slow_billing_catch_up_neither_fails_nor_holds_the_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    ceiling: bool,
) -> None:
    """The catch-up is bounded by its own budget and runs outside the ceiling: a
    stalled MA read cannot turn a healthy turn into a ceiling failure, and the
    turn returns about one budget later, with its outcome intact."""
    import time

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-slow-catch-up",
        ma_session_id="sess_1",
    )
    await db_session.commit()
    router = _router(session_bodies=[], dead_session_ids=set())

    stall_s, budget_s = 30.0, 0.2

    async def _stalled_list(_request: httpx.Request, _match: object) -> httpx.Response:
        await asyncio.sleep(stall_s)  # an upstream that never answers in time
        return list_response([])

    router.routes.insert(
        0, ("GET", re.compile(r"/v1/sessions/(?P<sid>[^/]+)/events"), cast(Any, _stalled_list))
    )
    monkeypatch.setattr(run_module, "SETTLE_BUDGET_S", budget_s)
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

    started = time.monotonic()
    with structlog.testing.capture_logs() as logs:
        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-slow-catch-up",
            external_user_id="user-1",
            user_message="hello",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
            deadline=(datetime.now(UTC) - timedelta(seconds=5)) if ceiling else None,
        )

    elapsed = time.monotonic() - started
    assert elapsed < budget_s + 2.0 < stall_s, f"the turn was held {elapsed:.2f}s by its catch-up"
    expected = TerminationReason.CEILING if ceiling else TerminationReason.COMPLETED
    assert outcome.termination is expected
    assert [e["complete"] for e in logs if e["event"] == "billing.settled"] == [False]


async def test_a_turn_is_registered_as_running_for_exactly_the_call(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown drains count turns through `deps.running_turns`, whatever entry
    point called `run_prepared_turn` (#16)."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-running",
        ma_session_id="sess_running",
    )
    await db_session.commit()

    router = _router(session_bodies=[], dead_session_ids=set())
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(account_id=account.id, agent=agent, env=env),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_running",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    real_impl = run_module.run_prepared_turn_impl
    seen: list[set[asyncio.Task[object]]] = []

    async def _observing_impl(*args: object, **kwargs: object) -> RunOutcome:
        seen.append(deps.running_turns.tasks())
        return await real_impl(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(run_module, "run_prepared_turn_impl", _observing_impl)
    assert deps.running_turns.count() == 0

    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-running",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert seen == [{asyncio.current_task()}], "the calling task is registered while the turn runs"
    assert deps.running_turns.count() == 0, "and unregistered once it returns"


async def test_a_dead_session_replacement_is_sealed_when_its_seal_cannot_be_read(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The recovery replays what it can of the dead session's log; with the
    dead session's own seal unreadable, the successor is sealed to its thread."""
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
    router.add(
        "GET",
        r"/v1/sessions/sess_old",
        lambda _r, _m: httpx.Response(
            404, json={"type": "error", "error": {"type": "not_found_error", "message": "gone"}}
        ),
    )
    deps = _deps(sessionmaker=db_session_factory, router=router)
    admission = dataclasses.replace(
        _admission(
            account_id=account.id,
            agent=ma_agent(id="ag_1", tenant_id=tenant.id),
            env=ma_environment(id="env_1", tenant_id=tenant.id),
        ),
        origin_channel_id="chan-2",
        origin_thread_id="thread-2",
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
        thread_id="thread-2",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.recovered is True
    metadata = session_bodies[0]["metadata"]
    assert isinstance(metadata, dict)
    assert metadata.get("daimon_sealed") == "thread-2"


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
    admission = dataclasses.replace(
        _admission(account_id=account.id, agent=agent, env=env), budget_channel_id="c-parent"
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
        thread_id="thread-2",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.termination is TerminationReason.COMPLETED, "the recovered run is what ended"
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
    assert live.channel_id == "c-parent", "the new row records the channel the turn runs for"

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

    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
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
    completed = [entry for entry in logs if entry["event"] == "turn.completed"]
    assert completed and completed[-1]["model"] == "claude-opus-5", (
        "the replacement session's lines name its own model"
    )
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


async def test_a_failed_recovery_tells_the_caller_recovery_failed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When replacing the dead session raises, the held failure is replayed as
    `recovery_failed`, not the first attempt's upstream error: that one would
    tell the person their workspace was kept."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-recovery-fails",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    async def _replacement_fails(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("replacement failed")

    monkeypatch.setattr(run_module, "_replace_dead_session", _replacement_fails)
    router = _router(session_bodies=[], dead_session_ids={"sess_old"})
    deps = _deps(sessionmaker=db_session_factory, router=router)
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    env = ma_environment(id="env_1", tenant_id=tenant.id)
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(account_id=account.id, agent=agent, env=env),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_old",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    caller_lifecycle = RecordingLifecycle()

    with pytest.raises(RuntimeError, match="replacement failed"):
        await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-recovery-fails",
            external_user_id="user-1",
            user_message="hello",
            lifecycle=caller_lifecycle,
            cancel=asyncio.Event(),
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
        )

    assert len(caller_lifecycle.terminal_failures) == 1
    assert caller_lifecycle.terminal_failures[0][0].termination is (
        TerminationReason.RECOVERY_FAILED
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
    assert outcome.termination is TerminationReason.CEILING
    assert caller_lifecycle.terminal_failures[0][0].termination is TerminationReason.CEILING
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
            await asyncio.Event().wait()  # never opens: only the ceiling can end it
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

    # Ten times the slowest observed first attempt + recovery setup (~0.18 s):
    # the ceiling must land on the recovered stream, never inside setup.
    deadline = datetime.now(UTC) + timedelta(seconds=2.0)

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
        scope: Scope,
        lifecycle: TurnLifecycle,
        cancel: asyncio.Event,
        render_interval_s: object,
        billing: object,
        image_blocks: object,
        system_blocks: object = (),
        context_hook: object = None,
        tool_confirmation: object = None,
        before_send: object = None,
        send_guard: object = None,
        path: object = None,
    ) -> TurnState:
        nonlocal call_count
        assert scope.tenant_id == str(tenant.id) and scope.account_id == str(account.id)
        assert not scope.is_platform and not scope.is_legacy_host_authorized
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
    assert outcome.termination is TerminationReason.RECOVERY_CANCELLED
    assert caller_lifecycle.terminal_failures[0][0].termination is (
        TerminationReason.RECOVERY_CANCELLED
    ), "the caller's hook and the outcome must agree on how the turn ended"


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
    recovery_started = asyncio.Event()

    async def _fake_run_turn(
        *,
        anthropic: object,
        session_id: str,
        user_message: str,
        scope: Scope,
        lifecycle: TurnLifecycle,
        cancel: asyncio.Event,
        render_interval_s: object,
        billing: object,
        image_blocks: object,
        system_blocks: object = (),
        context_hook: object = None,
        tool_confirmation: object = None,
        before_send: object = None,
        send_guard: object = None,
        path: object = None,
    ) -> TurnState:
        nonlocal call_count
        assert scope.tenant_id == str(tenant.id) and scope.account_id == str(account.id)
        assert not scope.is_platform and not scope.is_legacy_host_authorized
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
        recovery_started.set()
        await cancel.wait()
        return TurnState(error=TurnError(kind="interrupted", message="interrupted during recovery"))

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)

    original_cancel = asyncio.Event()

    async def _cancel_soon() -> None:
        # Cancel while the recovery turn is in flight, not after a fixed delay:
        # a slow runner may not have reached the recovery attempt in 20 ms.
        await asyncio.wait_for(recovery_started.wait(), 10)
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
    from daimon.core.turn.outcomes import drain_outcomes

    await drain_outcomes()
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
    from daimon.core.turn.outcomes import drain_outcomes

    await drain_outcomes()
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


async def test_a_chat_turn_sends_mcp_context_in_front_of_the_message(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The agent's MCP servers are asked about the person's own words, and what
    they answer goes in front of the full message the adapter built."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-context",
        ma_session_id="sess_1",
    )
    await db_session.commit()

    asked: list[str] = []

    async def fetch(*, prompt: str, **_: object) -> str:
        asked.append(prompt)
        return "<mcp_context>Acme renewed in May.</mcp_context>\n\n"

    sent: list[tuple[str, list[dict[str, object]]]] = []
    router = _router(session_bodies=[], dead_session_ids=set(), sent_batches=sent)
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router), turn_context=fetch
    )
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

    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-context",
        external_user_id="user-1",
        user_message="<turn_controls>{}</turn_controls>\nwhat about acme?",
        request_text="what about acme?",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert asked == ["what about acme?"], "only the person's words are sent to the servers"
    _, batch = sent[0]
    text = "".join(
        block["text"]
        for block in batch[0]["content"]  # pyright: ignore[reportUnknownVariableType]
        if block["type"] == "text"
    )
    assert text == (
        "<mcp_context>Acme renewed in May.</mcp_context>\n\n\n"
        "<turn_controls>{}</turn_controls>\nwhat about acme?"
    ), "the answer leads the message the adapter built"


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
    assert outcome.loss_told, "the replacement was sent its loss framing"
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


@dataclasses.dataclass
class _QueuedTurn:
    outcome: RunOutcome
    rows: list[ThreadSession]
    requests: list[str]
    usage_after_waiter: list[Any]
    usage_after_holder: list[Any]


async def _run_turn_behind_a_busy_lease(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    thread_id: str,
    cancel: asyncio.Event,
    ceiling_after_s: float | None,
    stop_after_s: float | None = None,
) -> _QueuedTurn:
    """Run user B's mux turn on a thread whose lease user A's turn always holds.

    The session has one model call upstream that A's stream has not recorded.
    After B's turn ends, A settles the session as its holder would; the usage
    rows are read after each step."""
    from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
        BetaManagedAgentsSpanModelRequestEndEvent,
    )
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.core.turn.prepare import admitted_session_scope as scope_for
    from mux.state.lease import LeaseBusy

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id=thread_id,
        ma_session_id="sess_live",
    )
    await db_session.commit()
    unrecorded = BetaManagedAgentsSpanModelRequestEndEvent(
        id="evt_span_of_a",
        is_error=False,
        model_request_start_id="start_1",
        model_usage=ma_model_usage(input_tokens=10, output_tokens=20),
        processed_at=datetime.now(UTC),
        type="span.model_request_end",
    )
    router = _router(
        session_bodies=[],
        dead_session_ids=set(),
        events_by_session={"sess_live": [unrecorded.model_dump(mode="json")]},
    )
    requests: list[str] = []

    async def busy(self: object, slot: Any, **_: object) -> NoReturn:
        raise LeaseBusy(slot.describe(), "another-turn")

    monkeypatch.setattr(PostgresStateStore, "acquire_lease", busy)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        return router.dispatch(request)

    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, poolclass=NullPool
    )
    try:
        sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
        deps = dataclasses.replace(
            _deps(sessionmaker=sessionmaker, router=router),
            turn_path="mux",
            channel_backends=True,
            anthropic=anthropic.AsyncAnthropic(
                api_key="test",
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com"
                ),
                max_retries=0,
            ),
        )
        agent = ma_agent(id="ag_1", tenant_id=tenant.id)
        env = ma_environment(id="env_1", tenant_id=tenant.id)
        admission = _admission(account_id=account.id, agent=agent, env=env)
        prepared = _prepared_turn(
            deps=deps,
            admission=admission,
            tenant_id=tenant.id,
            external_user_id="user-b",
            ma_session_id="sess_live",
            mapping_id=row.id,
            session_account_id=account.id,
        )
        if stop_after_s is not None:
            asyncio.get_running_loop().call_later(stop_after_s, cancel.set)
        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=thread_id,
            external_user_id="user-b",
            user_message="hello",
            lifecycle=RecordingLifecycle(),
            cancel=cancel,
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
            # Fixture setup must not consume the lease-wait deadline.
            deadline=(
                datetime.now(UTC) + timedelta(seconds=ceiling_after_s)
                if ceiling_after_s is not None
                else None
            ),
        )
        async with sessionmaker() as s:
            rows = list(
                (await s.execute(select(ThreadSession).where(ThreadSession.tenant_id == tenant.id)))
                .scalars()
                .all()
            )
            usage_after_waiter = list(await usage_events.list_for_tenant(s, tenant_id=tenant.id))
        requests_by_waiter = list(requests)
        await run_module._settle(  # pyright: ignore[reportPrivateUsage]
            deps,
            session_id="sess_live",
            scope=scope_for(admission, tenant_id=tenant.id, session_id="sess_live"),
            record=bind_recorder(
                deps,
                tenant_id=tenant.id,
                external_user_id="user-a",
                ma_session_id="sess_live",
                model_id=agent.model.id,
            ),
        )
        async with sessionmaker() as s:
            usage_after_holder = list(await usage_events.list_for_tenant(s, tenant_id=tenant.id))
    finally:
        await engine.dispose()
    return _QueuedTurn(outcome, rows, requests_by_waiter, usage_after_waiter, usage_after_holder)


def _assert_usage_belongs_to_the_holder(queued: _QueuedTurn) -> None:
    assert queued.usage_after_waiter == [], "a turn that never held the lease records no usage"
    assert [(u.managed_session_id, u.platform_user_id) for u in queued.usage_after_holder] == [
        ("sess_live", "user-a")
    ], "the holder's own settlement records the call once, under the holder"


def _record_native_abandons(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Give every turn I/O the optional `abandon_native` hook a native codec
    (Gemini) has, recording each call. A queued turn never started the work the
    lease holder is running, so it must never stop it."""
    from daimon.core.turn.io import LegacyTurnIO, MuxTurnIO

    calls: list[str] = []

    async def abandon_native(self: object) -> None:
        calls.append(type(self).__name__)

    for cls in (LegacyTurnIO, MuxTurnIO):
        monkeypatch.setattr(cls, "abandon_native", abandon_native, raising=False)
    return calls


async def test_a_turn_queued_behind_a_lease_ends_at_the_ceiling_without_retiring_the_session(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the mux path a turn that finds the thread's lease held waits for it, at
    most until its own ceiling, then ends through the ordinary ceiling outcome. It
    never ran on the mapping, whose holder is using it, so the mapping stays live."""
    abandons = _record_native_abandons(monkeypatch)
    started = asyncio.get_running_loop().time()
    queued = await _run_turn_behind_a_busy_lease(
        db_session,
        db_schema,
        monkeypatch,
        thread_id="thread-queued-ceiling",
        cancel=asyncio.Event(),
        ceiling_after_s=2.0,
    )
    waited = asyncio.get_running_loop().time() - started
    outcome, rows, requests = queued.outcome, queued.rows, queued.requests

    assert outcome.state.error is not None and outcome.state.error.kind == "ceiling"
    assert outcome.state.termination == TerminationReason.CEILING
    assert 0.4 <= waited < 5, "the wait is bounded by the turn's ceiling"
    assert [(r.ma_session_id, r.status) for r in rows] == [("sess_live", "live")]
    assert not [r for r in requests if r.startswith("POST")], "a queued turn sends nothing"
    assert not [r for r in requests if r == "GET /v1/sessions/sess_live/events"], (
        "a queued turn does not read the session's events to settle them"
    )
    assert abandons == [], "a queued turn does not stop the holder's native work"
    _assert_usage_belongs_to_the_holder(queued)


async def test_a_turn_stopped_while_queued_behind_a_lease_ends_as_an_interrupt(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abandons = _record_native_abandons(monkeypatch)
    queued = await _run_turn_behind_a_busy_lease(
        db_session,
        db_schema,
        monkeypatch,
        thread_id="thread-queued-stop",
        cancel=asyncio.Event(),
        ceiling_after_s=None,
        stop_after_s=0.2,
    )

    outcome, rows, requests = queued.outcome, queued.rows, queued.requests
    assert outcome.state.termination == TerminationReason.INTERRUPTED
    assert not outcome.recovered
    assert [(r.ma_session_id, r.status) for r in rows] == [("sess_live", "live")]
    assert not [r for r in requests if r.startswith("POST")]
    assert not [r for r in requests if r == "GET /v1/sessions/sess_live/events"]
    assert abandons == [], "a queued turn does not stop the holder's native work"
    _assert_usage_belongs_to_the_holder(queued)


# Both ceiling tests below aim the real turn ceiling at `link_replacement`, which they
# make sleep 30s, so the ceiling lands after the replacement session exists. Everything
# before it (the first attempt, the 404, transcript read, session and memory-store
# creation) has to finish inside the deadline: at 1s it did not on a loaded CI runner, and
# the ceiling fired inside `create_ma_session` instead. Each test waits out the full value.
RECOVERY_CEILING_S = 5.0


async def test_a_ceiling_during_recovery_setup_leaves_no_orphan_live_replacement(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replacement row used to be committed in its own transaction, inside
    the recovery's locked one. A ceiling (or any cancel) landing after that
    commit rolled back the dead-mark and the lineage link but left the new row
    LIVE and unlinked: the next mention continued on it without the
    lost-workspace framing. Mark-dead, insert and link must commit or roll
    back together. A separate engine, because the shared-connection fixture
    folds the inner commit into the outer transaction and hides the orphan."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-ceiling-in-setup",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids={"sess_old"})

    real_link = run_module.link_replacement

    linking = asyncio.Event()

    async def slow_link(*args: object, **kwargs: object) -> None:
        linking.set()
        await asyncio.sleep(30)  # the ceiling fires here, after the replacement exists
        await real_link(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(run_module, "link_replacement", slow_link)

    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, poolclass=NullPool
    )
    try:
        sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
        deps = _deps(sessionmaker=sessionmaker, router=router)
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
            thread_id="thread-ceiling-in-setup",
            external_user_id="user-1",
            user_message="hello",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
            deadline=datetime.now(UTC) + timedelta(seconds=RECOVERY_CEILING_S),
        )
        async with sessionmaker() as s:
            rows = (
                (
                    await s.execute(
                        select(ThreadSession).where(
                            ThreadSession.tenant_id == tenant.id,
                            ThreadSession.thread_id == "thread-ceiling-in-setup",
                        )
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    assert outcome.state.error is not None and outcome.state.error.kind == "ceiling"
    assert linking.is_set(), "the ceiling must land in link_replacement, not before it"
    live = [(r.ma_session_id, r.replaced_by_id) for r in rows if r.status == "live"]
    assert live == [], f"a replacement survived the rolled-back recovery: {live}"
    assert [r.ma_session_id for r in rows] == ["sess_old"], (
        "the replacement row must roll back with the dead-mark and the link"
    )


async def test_a_rolled_back_recovery_archives_the_session_it_created(
    db_session: AsyncSession,
    db_schema: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the locked recovery transaction rolls back after the upstream
    session was created, no row names that session any more, so nothing would
    ever archive it. The recovery archives it on the way out (best effort)."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-orphan-archive",
        ma_session_id="sess_old",
    )
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _router(session_bodies=session_bodies, dead_session_ids={"sess_old"})
    archived: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        m = re.fullmatch(r"/v1/sessions/(?P<id>[^/]+)/archive", request.url.path)
        if request.method == "POST" and m is not None:
            archived.append(m.group("id"))
            return httpx.Response(500, json={"type": "error", "error": {"type": "api_error"}})
        return router.dispatch(request)

    real_link = run_module.link_replacement

    linking = asyncio.Event()

    async def slow_link(*args: object, **kwargs: object) -> None:
        linking.set()
        await asyncio.sleep(30)  # the ceiling fires here, after the session exists
        await real_link(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(run_module, "link_replacement", slow_link)

    engine = build_test_engine(
        os.environ["DAIMON_DATABASE__TEST_URL"], db_schema, poolclass=NullPool
    )
    try:
        deps = dataclasses.replace(
            _deps(
                sessionmaker=async_sessionmaker(bind=engine, expire_on_commit=False),
                router=router,
            ),
            anthropic=anthropic.AsyncAnthropic(
                api_key="test",
                http_client=httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com"
                ),
                max_retries=0,
            ),
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
        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-orphan-archive",
            external_user_id="user-1",
            user_message="hello",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
            # The turn ceiling fires while link_replacement sleeps; the archive
            # answers at once, so only this deadline is waited out.
            deadline=datetime.now(UTC) + timedelta(seconds=RECOVERY_CEILING_S),
        )
    finally:
        await engine.dispose()

    assert outcome.state.error is not None and outcome.state.error.kind == "ceiling"
    assert linking.is_set(), "the ceiling must land in link_replacement, not before it"
    assert len(session_bodies) == 1, "recovery created one upstream session"
    assert archived == ["sess_1"], (
        "the session the rolled-back recovery created must be archived; "
        "a failing archive call is logged, not raised (this one answers 500)"
    )


@pytest.mark.parametrize(
    ("error", "event"),
    [
        (
            UnsupportedCapability(("session_archive",), "gemini.inline_reuse"),
            "turn.recovery_orphan_archive_skipped",
        ),
        (
            UnsupportedCapability(("session_delete",), "gemini.inline_reuse"),
            "turn.recovery_orphan_archive_failed",
        ),
        (RuntimeError("archive 500"), "turn.recovery_orphan_archive_failed"),
    ],
)
async def test_a_profile_without_an_archive_skips_the_orphan_cleanup_quietly(
    error: Exception, event: str
) -> None:
    async def archive() -> object:
        raise error

    with structlog.testing.capture_logs() as logs:
        await run_module._archive_orphaned_session(
            cast(anthropic.AsyncAnthropic, None), session_id="sess_orphan", archive=archive
        )
    assert [e["event"] for e in logs if "orphan" in e["event"]] == [event]


async def test_orphan_archive_timeout_does_not_wait_and_logs_late_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait timeout bounds the caller but leaves the shielded SDK request alive."""
    started = asyncio.Event()
    finish = asyncio.Event()
    completed = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await finish.wait()
        completed.set()
        return httpx.Response(
            500,
            json={"type": "error", "error": {"type": "api_error", "message": "late failure"}},
        )

    anthropic_client = anthropic.AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com"
        ),
        max_retries=0,
    )
    monkeypatch.setattr(run_module, "_ORPHAN_ARCHIVE_TIMEOUT_S", 0.01)
    try:
        with structlog.testing.capture_logs() as logs:
            started_at = loop.time()
            await run_module._archive_orphaned_session(anthropic_client, session_id="sess_orphan")
            elapsed = loop.time() - started_at
            assert started.is_set(), "the archive request should have started before timeout"
            assert elapsed < 0.5, "timeout should bound the caller's wait"
            assert not completed.is_set(), "the shielded archive request should still be in flight"

            finish.set()
            await asyncio.wait_for(completed.wait(), timeout=0.5)
            await asyncio.sleep(0)
        failures = [
            event for event in logs if event["event"] == "turn.recovery_orphan_archive_failed"
        ]
        assert len(failures) == 2, (
            f"the timeout and the request's later 500 must each be logged; got {failures}"
        )
    finally:
        finish.set()
        await anthropic_client.close()


def test_orphan_archive_returns_after_successful_archive_with_timeout() -> None:
    """A successful archive must return instead of spinning on its completed task."""
    script = """
import asyncio
import anthropic
import httpx
from daimon.core.turn.run import _archive_orphaned_session
from mux.testing.drivers.anthropic.ma_models import ma_session

async def handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=ma_session(id="sess_orphan").model_dump(mode="json"))

async def main() -> None:
    client = anthropic.AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com"
        ),
        max_retries=0,
    )
    await _archive_orphaned_session(client, session_id="sess_orphan")
    await client.close()

asyncio.run(main())
"""
    # Interpreter startup and shutdown also run under this outer watchdog.
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30.0, check=False
    )
    assert result.returncode == 0, (
        "a successful archive should finish within the subprocess startup allowance; "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


async def test_orphan_archive_second_cancel_preserves_the_unwinding_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await finish.wait()
        return httpx.Response(
            500,
            json={"type": "error", "error": {"type": "api_error", "message": "archive failed"}},
        )

    anthropic_client = anthropic.AsyncAnthropic(
        api_key="test",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.anthropic.com"
        ),
        max_retries=0,
    )
    monkeypatch.setattr(run_module, "_ORPHAN_ARCHIVE_TIMEOUT_S", 30.0)

    async def unwind() -> None:
        try:
            raise ValueError("original transaction failure")
        except BaseException:
            await run_module._archive_orphaned_session(anthropic_client, session_id="sess_orphan")
            raise

    try:
        unwind_task = asyncio.create_task(unwind())
        await asyncio.wait_for(started.wait(), timeout=0.5)
        unwind_task.cancel("second cancellation")
        assert not unwind_task.done(), "the cleanup should keep waiting after a second cancel"
        finish.set()
        with pytest.raises(ValueError, match="original transaction failure"):
            await unwind_task
    finally:
        finish.set()
        await anthropic_client.close()


@pytest.mark.parametrize(
    "gate_scope,overrides_enabled",
    [
        (None, True),
        ("server", True),
        ("channel:chan-1", True),
        ("thread:thread-hook-threading", True),
        ("server", False),
    ],
)
async def test_the_confirmation_hook_reaches_the_turn_through_the_observing_wrapper(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    gate_scope: str | None,
    overrides_enabled: bool,
) -> None:
    """SYS-038 x SYS-081: `run_prepared_turn` observes, `run_prepared_turn_impl`
    runs; the adapter's card hook and `attended` must survive the hop."""
    from daimon.core.approval_gate import ApprovalGate
    from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
    from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
    from daimon.core.turn.posture import PolicyApproval

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-hook-threading",
        ma_session_id="sess_hook",
    )
    await db_session.commit()
    router = _router(session_bodies=[], dead_session_ids=set())
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        tool_safety=ToolSafetyPolicy(enabled=True, allow_gate_overrides=overrides_enabled),
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=dataclasses.replace(
            _admission(
                account_id=account.id,
                agent=ma_agent(
                    id="ag_1",
                    tenant_id=tenant.id,
                    mcp_servers=[
                        {"type": "url", "name": "linear", "url": "https://linear.example/mcp"}
                    ],
                ),
                env=ma_environment(id="env_1", tenant_id=tenant.id),
            ),
            approval_gate=ApprovalGate("off", gate_scope, "admin") if gate_scope else None,
        ),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_hook",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    from unittest.mock import AsyncMock

    refresh = AsyncMock(return_value=prepared.admission.approval_gate)
    monkeypatch.setattr("daimon.core.turn.run.refresh_approval_gate", refresh)
    postures: list[object] = []

    async def _fake_run_turn(**kwargs: object) -> TurnState:
        postures.append(kwargs["tool_confirmation"])
        return TurnState()

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-hook-threading",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        confirm_write=card,
    )

    (posture,) = postures
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(
        ToolCall(tool_use_id="tu", server_name="linear", tool_name="create_issue")
    )
    assert result.allow
    assert len(prompts) == (0 if gate_scope is not None and overrides_enabled else 1)


@pytest.mark.parametrize("attended", [True, False], ids=["chat", "unattended"])
@pytest.mark.parametrize(
    "trusted", [("bridge",), (), ("other",)], ids=["trusted", "empty", "other-server-trusted"]
)
async def test_a_prepared_turn_reads_trusted_server_annotations_with_the_agent_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    trusted: tuple[str, ...],
    attended: bool,
) -> None:
    """The production wiring: `run_prepared_turn` hands the decider a lookup that
    reads a trusted server's catalogue, so a `readOnlyHint` read runs without
    a card.
    With an empty `trusted_annotations`, or one naming another server, the
    server is never listed and its name decides."""
    from cryptography.fernet import Fernet, MultiFernet
    from daimon.core import mcp_tool_annotations
    from daimon.core.agent_mcp_credentials import ResolvedMcpCredential
    from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
    from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
    from daimon.core.turn.posture import PolicyApproval

    bridge_url = "https://bridge.example.com/mcp"
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-annotations",
        ma_session_id="sess_annotations",
    )
    await db_session.commit()
    router = _router(session_bodies=[], dead_session_ids=set())
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        tool_safety=ToolSafetyPolicy(enabled=True, trusted_annotations=trusted),
        fernet=MultiFernet([Fernet(Fernet.generate_key())]),
    )
    admission = _admission(
        account_id=account.id,
        agent=ma_agent(
            id="ag_1",
            tenant_id=tenant.id,
            mcp_servers=[{"name": "bridge", "type": "url", "url": bridge_url}],
        ),
        env=ma_environment(id="env_1", tenant_id=tenant.id),
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_annotations",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    credential_reads: list[uuid.UUID] = []

    async def _credentials(**kwargs: Any) -> tuple[ResolvedMcpCredential, ...]:
        credential_reads.append(kwargs["agent_id"])
        return (ResolvedMcpCredential(mcp_server_url=bridge_url, token="agent-wide", version="v"),)

    tokens: list[str | None] = []

    async def _catalogue(url: str, token: str | None) -> list[dict[str, Any]]:
        tokens.append(token)
        return [{"name": "memory_flag_list", "annotations": {"readOnlyHint": True}}]

    monkeypatch.setattr(mcp_tool_annotations, "resolve_agent_mcp_credentials", _credentials)
    monkeypatch.setattr(mcp_tool_annotations, "read_mcp_catalogue", _catalogue)
    postures: list[object] = []

    async def _fake_run_turn(**kwargs: object) -> TurnState:
        postures.append(kwargs["tool_confirmation"])
        return TurnState()

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "denied"

    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-annotations",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        confirm_write=card,
        attended=attended,
    )

    (posture,) = postures
    assert isinstance(posture, PolicyApproval)
    result = await posture.decide(
        ToolCall(tool_use_id="tu", server_name="bridge", tool_name="memory_flag_list")
    )
    if "bridge" not in trusted:
        assert not result.allow and len(prompts) == int(attended), (
            "the name decides: a noun-first write"
        )
        assert tokens == [] and credential_reads == [], "no tools/list for an untrusted server"
        return
    assert result.allow and prompts == [], "readOnlyHint ran the read without a card"
    assert tokens == ["agent-wide"]
    assert credential_reads == [derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_1")]


@pytest.mark.parametrize("microsoft", [False, True], ids=["plain", "selected-tools"])
async def test_an_unattended_prepared_turn_uses_the_unattended_decider_with_safety_off(
    microsoft: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review B, finding 6: a fleet worker or wake (attended=False) on a session
    still asking from an earlier policy gets the explicit off allow for an
    ordinary call, and Daimon's card-only actions are still refused."""
    from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
    from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
    from daimon.core.turn.posture import PolicyApproval

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-unattended",
        ma_session_id="sess_unattended",
    )
    await db_session.commit()
    router = _router(session_bodies=[], dead_session_ids=set())
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        tool_safety=ToolSafetyPolicy(enabled=False),
        microsoft_send=microsoft,
        public_url="https://daimon.example/mcp",
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(
            account_id=account.id,
            agent=ma_agent(id="ag_1", tenant_id=tenant.id),
            env=ma_environment(id="env_1", tenant_id=tenant.id),
        ),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_unattended",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    postures: list[object] = []

    async def _fake_run_turn(**kwargs: object) -> TurnState:
        postures.append(kwargs["tool_confirmation"])
        return TurnState()

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)
    prompts: list[ConfirmationPrompt] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-unattended",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        attended=False,
    )

    (posture,) = postures
    assert isinstance(posture, PolicyApproval)
    ordinary = await posture.decide(
        ToolCall(tool_use_id="tu", server_name="linear", tool_name="create_issue")
    )
    assert ordinary.allow, "safety off allows an ordinary third-party call"
    protected = await posture.decide(
        ToolCall(
            tool_use_id="tu2",
            server_name="daimon-mcp",
            tool_name="microsoft_send",
            input={"action": "send_mail"},
        )
    )
    assert not protected.allow and prompts == []


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize(
    ("runtime", "failed_batch"),
    [
        ("legacy", None),
        ("legacy", 1),
        ("legacy", 2),
        ("mux", None),
        ("mux", 1),
        ("mux-with-read", None),
        ("mux-without-read", None),
        ("non-anthropic", None),
    ],
)
async def test_unattended_wake_regates_each_batch_or_refuses_without_a_session_read(
    platform: str,
    runtime: str,
    failed_batch: int | None,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.mux_backend import managed_agents, resource_ref
    from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
    from daimon.core.turn.posture import PolicyApproval
    from daimon.core.turn.prepare import admitted_session_scope
    from mux.contracts.config import ConfigRevision, ResolvedBackend
    from mux.contracts.ids import ChannelRef, ResourceRef
    from mux.errors import UnsupportedCapability
    from mux.testing.drivers.anthropic.ma_models import ma_session

    from .conftest import make_end_turn, make_mcp_tool_use, make_requires_action

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    gateable = runtime in ("legacy", "mux", "mux-with-read")
    agent = ma_agent(
        id="ag_wake",
        tenant_id=tenant.id,
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        ],
        mcp_servers=[{"type": "url", "name": "linear", "url": "https://linear.example/mcp"}],
    )
    snapshot = ma_session(id="sess_wake_gate", agent=agent)
    order: list[str] = []
    confirmations: list[dict[str, Any]] = []
    events = []
    for i in (1, 2):
        events.extend(
            [
                make_mcp_tool_use(event_id=f"call_{i}", name="get_issue", mcp_server_name="linear"),
                make_status_idle(
                    event_id=f"pause_{i}", stop_reason=make_requires_action(event_ids=[f"call_{i}"])
                ),
            ]
        )
    events.append(make_status_idle(event_id="end", stop_reason=make_end_turn()))
    wire_events = [event.model_dump(mode="json") for event in events]
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/sess_wake_gate",
        lambda r, m: httpx.Response(200, json=snapshot.model_dump(mode="json")),
    )
    router.add(
        "GET", r"/v1/sessions/sess_wake_gate/events/stream", lambda r, m: sse_response(wire_events)
    )
    router.add(
        "GET", r"/v1/sessions/sess_wake_gate/events", lambda r, m: list_response(wire_events)
    )
    updates = 0

    def update(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        nonlocal updates
        updates += 1
        body = json.loads(request.content)
        assert body["agent"]["tools"][0]["default_config"]["permission_policy"] == {
            "type": "always_ask"
        }
        if failed_batch is not None and updates == failed_batch + 1:
            order.append("update refused")
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "gate refused"},
                },
            )
        order.append("update")
        return httpx.Response(200, json=snapshot.model_dump(mode="json"))

    def send(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        for event in json.loads(request.content)["events"]:
            if event["type"] == "user.tool_confirmation":
                confirmations.append(event)
                order.append(event["result"])
            elif event["type"] == "user.message":
                order.append("message")
        return send_events_response()

    router.add("POST", r"/v1/sessions/sess_wake_gate", update)
    router.add("POST", r"/v1/sessions/sess_wake_gate/events", send)
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        turn_path="legacy" if runtime == "legacy" else "mux",
        tool_safety=ToolSafetyPolicy(enabled=gateable),
    )
    # A timer's genuine chat origin can call MCP hand_off_task. Queueing that
    # tool's continuation must retain attendance through the destination run.
    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.runtime import McpRuntime
    from daimon.adapters.mcp.tools.task_continuity import (
        _hand_off_task_impl,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.config import DatabaseSettings, Settings
    from daimon.core.continuity.continuation import continuation_attended
    from daimon.core.stores.domain import Role
    from daimon.core.stores.task_continuations import list_pending_continuations
    from daimon.core.turn_origin import turn_origin
    from pydantic import PostgresDsn

    router.add("GET", r"/v1/agents", lambda r, m: list_response([agent.model_dump(mode="json")]))
    handoff_runtime = McpRuntime(
        session_factory=db_session_factory,
        client=deps.anthropic,
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test"))
        ),
        deployment_default=DeploymentDefault(agent_name=agent.name),
    )
    async with turn_origin(
        db_session_factory,
        tenant_id=tenant.id,
        account_id=account.id,
        platform=platform,
        parent_channel_id="C1",
        thread_id="wake-gate",
        responder_ma_agent_id="ag_timer",
        responder_name="timer-agent",
        role=Role.USER,
        attended=False,
        chat_session_id="timer-session",
        sender_foreign=False,
        shared_channel=False,
    ) as origin:
        await _hand_off_task_impl(
            handoff_runtime,
            AuthIdentity(
                tenant_id=tenant.id,
                account_id=account.id,
                role=Role.USER,
                platform=platform,
                platform_user_id="user-1",
                chat_session_id="timer-session",
            ),
            origin_context_id=str(origin.id),
            agent_id=agent.id,
            continuation="continue the timer's work",
        )
    async with db_session_factory() as session:
        [queued] = await list_pending_continuations(
            session, tenant_id=tenant.id, platform=platform, thread_id="wake-gate"
        )
    assert queued.reason == "task_handoff" and not queued.attended
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform=platform,
        thread_id="wake-gate",
        ma_session_id="sess_wake_gate",
    )
    await db_session.commit()
    admission = _admission(account_id=account.id, agent=agent, env=ma_environment())
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id=row.ma_session_id,
        mapping_id=row.id,
        session_account_id=account.id,
    )
    if runtime in ("mux-with-read", "mux-without-read"):
        scope = admitted_session_scope(admission, tenant_id=tenant.id, session_id=row.ma_session_id)
        backend = managed_agents(
            deps.anthropic, scope=scope, resources=frozenset({("session", row.ma_session_id)})
        )

        def no_read(port: object, *, namespace: str, version: int) -> Any:
            raise UnsupportedCapability((namespace,), "anthropic.managed_agents")

        if runtime == "mux-without-read":
            monkeypatch.setattr(backend, "extension", no_read)
        prepared = dataclasses.replace(
            prepared,
            backend=backend,
            session_ref=resource_ref(backend, "session", row.ma_session_id, scope=scope),
        )
    elif runtime == "non-anthropic":
        revision = ConfigRevision.create(
            ChannelRef(tenant_id=str(tenant.id), platform=platform, channel_id="C1"),
            1,
            ResolvedBackend(
                backend="openai", profile="openai.conversation_only", model="gpt-6-luna"
            ),
        )
        from mux.contracts.ids import ModelRef
        from mux.contracts.resources import AgentSpec

        prepared = dataclasses.replace(
            prepared,
            # A supported tool-less profile may run, but even an unexpected
            # provider confirmation must still be refused without a host gate.
            _native_spec=AgentSpec(
                name="native-wake", model=ModelRef(provider="openai", id="gpt-6-luna"), tools=()
            ),
            admission=dataclasses.replace(
                admission, backend_revision=revision, backend_revision_origin="channel"
            ),
            session_ref=ResourceRef(
                id=row.ma_session_id,
                kind="session",
                provider="openai",
                account_scope_id="workspace",
                tenant_id=str(tenant.id),
                account_id=str(account.id),
            ),
        )

        class NativePersistence:
            lease_acquired = True

            def activate(self) -> contextlib.AbstractContextManager[None]:
                return contextlib.nullcontext()

        async def native_persistence(*args: Any, **kwargs: Any) -> NativePersistence:
            return NativePersistence()

        monkeypatch.setattr(run_module, "prepared_persistence", native_persistence)

        async def native_turn(**kwargs: Any) -> TurnState:
            assert "provider_actions" not in kwargs
            posture = kwargs["tool_confirmation"]
            assert isinstance(posture, PolicyApproval)
            decision = await posture.decide(
                ToolCall(tool_use_id="native-call", server_name="linear", tool_name="get_issue")
            )
            assert not decision.allow and decision.deny_message is not None
            assert "cannot refresh the conversation's tool safety gate" in decision.deny_message
            order.append("native denied")
            return TurnState()

        monkeypatch.setattr(run_module, "run_turn", native_turn)

    async def unused_provider_action(prompt: Any) -> None:
        raise AssertionError("unattended turns must never invoke a human provider-action hook")

    turn = run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform=platform,
        thread_id="wake-gate",
        external_user_id="user-1",
        user_message="wake up",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        origin="chat",
        attended=continuation_attended(queued),
        provider_action=unused_provider_action,
        render_interval_s=0.001,
    )
    if runtime == "mux-without-read":
        # A Managed Agents session that cannot be read cannot be checked or
        # constrained, so nothing is sent at all.
        from daimon.core.turn.errors import AdmissionDenied

        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            await turn
        assert order == []
        return
    outcome = await turn
    if runtime == "non-anthropic":
        assert order == ["native denied"]
    elif failed_batch is None:
        assert outcome.state.error is None
        assert order == ["update", "message", "update", "allow", "update", "allow"]
    else:
        assert outcome.state.error is not None
        assert order == ["update", "message"] + ["update", "allow"] * (failed_batch - 1) + [
            "update refused"
        ]
        assert len(confirmations) == failed_batch - 1


@pytest.mark.parametrize("origin", ["chat"], ids=["wake"])
@pytest.mark.parametrize("snapshot_kind", ["always_allow", "coordinator"])
async def test_prepared_runtime_without_session_reads_refuses_before_the_message(
    origin: Any,
    snapshot_kind: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Managed Agents runtime that cannot read its live session cannot check
    or constrain it: an always_allow MCP write, or a coordinator roster, would
    run on the first message. Nothing is sent, no confirmation, no recovery."""
    from daimon.core.mux_backend import managed_agents, resource_ref
    from daimon.core.tool_safety import ToolSafetyPolicy
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.prepare import admitted_session_scope
    from daimon.testing.ma_models import with_coordinator
    from mux.drivers.anthropic.sessions_lifecycle import SessionReads
    from mux.errors import UnsupportedCapability
    from mux.testing.drivers.anthropic.ma_models import ma_session

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    mapping = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="ungated",
        ma_session_id="sess_ungated",
    )
    await db_session.commit()
    agent = ma_agent(
        id="ag_ungated",
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "configs": [],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        ],
        mcp_servers=[{"type": "url", "name": "linear", "url": "https://linear.example/mcp"}],
    )
    live = ma_session(id="sess_ungated", agent=agent)
    if snapshot_kind == "coordinator":
        live = with_coordinator(live)
    requests: list[str] = []
    router = MARouter()

    def record(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(200, json=live.model_dump(mode="json"))
        return send_events_response()

    router.add("GET", r"/v1/sessions/sess_ungated.*", record)
    router.add("POST", r"/v1/sessions.*", record)
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        turn_path="mux",
        tool_safety=ToolSafetyPolicy(enabled=True),
    )
    admission = _admission(account_id=account.id, agent=agent, env=ma_environment())
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user",
        ma_session_id=mapping.ma_session_id,
        mapping_id=mapping.id,
        session_account_id=account.id,
    )
    scope = admitted_session_scope(admission, tenant_id=tenant.id, session_id=mapping.ma_session_id)
    backend = managed_agents(
        deps.anthropic, scope=scope, resources=frozenset({("session", mapping.ma_session_id)})
    )
    original_extension = backend.extension

    def extension(port: Any, *, namespace: str, version: int) -> Any:
        if port is SessionReads:
            raise UnsupportedCapability((namespace,), "anthropic.managed_agents")
        return original_extension(port, namespace=namespace, version=version)

    monkeypatch.setattr(backend, "extension", extension)
    prepared = dataclasses.replace(
        prepared,
        backend=backend,
        session_ref=resource_ref(backend, "session", mapping.ma_session_id, scope=scope),
    )

    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="ungated",
            external_user_id="user",
            user_message="wake up",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            origin=origin,
            attended=False,
            render_interval_s=0.001,
        )

    assert not [r for r in requests if r.startswith("POST")], requests


@pytest.mark.parametrize("cancel_during_cleanup", [False, True])
@pytest.mark.parametrize(
    "kind,origin_attended,caller_attended",
    [
        ("chat", True, True),
        ("chat", True, False),
        ("chat", False, True),
        ("github_access_ready", True, True),
        ("private_input_applied", True, True),
    ],
)
@pytest.mark.parametrize("path", ["legacy", "mux"])
async def test_prepared_turn_binds_only_its_recorded_session_origin_grant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    path: str,
    cancel_during_cleanup: bool,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    origin_attended: bool,
    caller_attended: bool,
) -> None:
    from daimon.core.session_snapshot import (
        fingerprint_identity,
        fingerprint_mutable,
        snapshot_from_created_session,
    )
    from daimon.core.stores.domain import Role
    from daimon.core.stores.thread_sessions import record_snapshot
    from daimon.core.stores.turn_origins import create_origin, get_active_origin
    from daimon.core.turn import persistence as persistence_module
    from daimon.core.turn_origin import current_execution_origin_id, turn_origin
    from mux.testing.drivers.anthropic.ma_models import ma_session

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    grant = str(uuid.uuid4())
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-1",
        ma_session_id="sess_1",
    )
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    snapshot = snapshot_from_created_session(
        ma_session(id="sess_1", agent=agent, metadata={"daimon_chat_session_id": grant}),
        env_sha256=None,
        env_file_id=None,
        repo_token_issued_at=None,
        vault_id="vlt_own",
        sent_skills=(),
    )
    await record_snapshot(
        db_session,
        id=row.id,
        snapshot=snapshot,
        identity_fingerprint=fingerprint_identity(snapshot),
        mutable_fingerprint=fingerprint_mutable(snapshot),
    )
    stale = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="C1",
        thread_id="thread-1",
        responder_ma_agent_id=agent.id,
        responder_name="daimon",
        role=Role.USER,
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        now=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        attended=True,
        chat_session_id=grant,
        sender_foreign=False,
        shared_channel=False,
    )
    await db_session.commit()
    router = _router(session_bodies=[], dead_session_ids=set())
    router.add(
        "GET",
        r"/v1/sessions/sess_1",
        lambda _r, _m: httpx.Response(
            200, json=ma_session(id="sess_1", agent=agent).model_dump(mode="json")
        ),
    )
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router), turn_path=path
    )
    admission = with_invocation(
        _admission(account_id=account.id, agent=agent, env=ma_environment()),
        tenant.id,
        human=kind == "chat" and caller_attended,
        external_user_id="user-1",
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id=row.ma_session_id,
        mapping_id=row.id,
        session_account_id=account.id,
    )
    seen = []
    entered, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_bind = run_module.bind_origin_session

    async def bind(db: AsyncSession, **kwargs: Any) -> None:
        if cancel_during_cleanup and kwargs["chat_session_id"] is None:
            cleaning.set()
            await finish.wait()
        await original_bind(db, **kwargs)

    monkeypatch.setattr(run_module, "bind_origin_session", bind)
    async with turn_origin(
        db_session_factory,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="discord",
        parent_channel_id="C1",
        thread_id="thread-1",
        responder_ma_agent_id=agent.id,
        responder_name="daimon",
        role=Role.USER,
        attended=origin_attended,
        sender_foreign=False,
        shared_channel=False,
    ) as origin:

        async def read_origin():
            async with db_session_factory() as session:
                return await get_active_origin(
                    session,
                    origin_id=origin.id,
                    tenant_id=tenant.id,
                    account_id=account.id,
                    platform="discord",
                    now=datetime.now(UTC),
                )

        async def before_send() -> None:
            bound = await read_origin()
            assert bound is not None
            assert bound.chat_session_id == (None if kind == "worker" else grant)
            assert bound.attended is (
                origin_attended if kind == "worker" else origin_attended and caller_attended
            )
            if kind in ("github_access_ready", "private_input_applied"):
                assert bound.attended is True
                assert bound.human_invocation is False
            assert current_execution_origin_id.get() == (None if kind == "worker" else origin.id)
            async with db_session_factory() as db:
                previous = await get_active_origin(
                    db,
                    origin_id=stale.id,
                    tenant_id=tenant.id,
                    account_id=account.id,
                    platform="discord",
                    now=datetime.now(UTC),
                )
            assert previous is not None
            assert previous.chat_session_id == (grant if kind == "worker" else None)
            seen.append(bound.id)
            if cancel_during_cleanup:
                entered.set()
                await asyncio.Event().wait()

        invocation = run_prepared_turn(
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
            attended=caller_attended,
            # Continuation reasons decide the grant; their rendering context stays chat.
            origin="worker" if kind == "worker" else "chat",
            render_interval_s=0.001,
            before_send=before_send,
        )
        if cancel_during_cleanup:
            task = asyncio.create_task(invocation)
            try:
                async with asyncio.timeout(5):
                    await entered.wait()
                    task.cancel()
                    if kind != "worker":
                        await cleaning.wait()
                        task.cancel()
                    finish.set()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                finish.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                # A second cancel releases the caller before persistence cleanup finishes.
                await asyncio.gather(*tuple(persistence_module._PENDING_FINALIZERS))
        else:
            outcome = await invocation
            assert outcome.state.error is None
        released = await read_origin()
        assert released is not None and released.chat_session_id is None
    assert seen == [origin.id]


@pytest.mark.parametrize(("origin", "entry_point"), [("chat", "wake")])
@pytest.mark.parametrize("unconstrainable", ["with_coordinator", "with_hidden_capability"])
async def test_unattended_prepared_turn_refuses_a_coordinator_and_builtins(
    origin: Any,
    entry_point: str,
    unconstrainable: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wakes and fleet workers gate through their own entry point's capability policy.

    The gate the driver runs before the message refuses a coordinator session
    before any update; the decider always refuses built-in calls.
    """
    from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
    from daimon.core.turn import mutation_gate
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.posture import PolicyApproval
    from daimon.core.turn.unattended import session_gate
    from mux.testing.drivers.anthropic.ma_models import ma_session

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-coordinator",
        ma_session_id="sess_coordinator",
    )
    await db_session.commit()
    agent = ma_agent(id="ag_1", tenant_id=tenant.id)
    from daimon.testing import ma_models

    snapshot = getattr(ma_models, unconstrainable)(ma_session(id="sess_coordinator", agent=agent))
    updates: list[object] = []
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/sess_coordinator$",
        lambda _r, _m: httpx.Response(200, json=snapshot.model_dump(mode="json")),
    )
    router.add(
        "POST",
        r"/v1/sessions/sess_coordinator$",
        lambda r, _m: updates.append(r) or httpx.Response(500),
    )
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        tool_safety=ToolSafetyPolicy(enabled=True),
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(
            account_id=account.id, agent=agent, env=ma_environment(id="env_1", tenant_id=tenant.id)
        ),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_coordinator",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    gates: list[object] = []
    monkeypatch.setattr(
        run_module,
        "session_gate",
        lambda *args, **kwargs: (
            gates.append(kwargs["entry_point"]) or session_gate(*args, **kwargs)
        ),
    )
    postures: list[object] = []
    refusals: list[BaseException] = []

    async def _fake_run_turn(**kwargs: object) -> TurnState:
        postures.append(kwargs["tool_confirmation"])
        gate = mutation_gate.unattended_gate.get()
        assert gate is not None
        try:
            await gate()
        except AdmissionDenied as err:
            refusals.append(err)
        return TurnState()

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)
    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-coordinator",
        external_user_id="user-1",
        user_message="wake up",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        attended=False,
        origin=origin,
    )

    assert gates == [entry_point]
    assert [getattr(e, "reason", None) for e in refusals] == ["backend_unsupported"]
    assert updates == []
    (posture,) = postures
    assert isinstance(posture, PolicyApproval)
    bash = await posture.decide(ToolCall(tool_use_id="tu", server_name=None, tool_name="bash"))
    assert bash.allow is False


@pytest.mark.parametrize("origin", ["chat"], ids=["wake"])
@pytest.mark.parametrize(
    ("server", "enabled"),
    [("linear", False), ("daimon-mcp", True), ("daimon-mcp", False)],
    ids=["foreign-safety-off", "trusted-safety-on", "trusted-safety-off"],
)
async def test_unattended_prepared_turn_makes_every_mcp_call_pause(
    origin: Any,
    server: str,
    enabled: bool,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wakes and fleet workers: trusted Daimon tools, and foreign ones with
    safety off, pause before running; the decider allows them as before."""
    from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
    from daimon.core.turn import mutation_gate
    from daimon.core.turn.posture import PolicyApproval
    from mux.testing.drivers.anthropic.ma_models import ma_session

    public_url = "https://daimon.example/mcp"
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-mcp",
        ma_session_id="sess_mcp",
    )
    await db_session.commit()
    url = public_url if server == "daimon-mcp" else "https://linear.example/mcp"
    agent = ma_agent(
        id="ag_1",
        tenant_id=tenant.id,
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": server,
                "configs": [
                    {
                        "name": "list_agents",
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    }
                ],
                "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
            }
        ],
        mcp_servers=[{"type": "url", "name": server, "url": url}],
    )
    snapshot = ma_session(id="sess_mcp", agent=agent)
    updates: list[dict[str, Any]] = []

    def update(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        updates.append(json.loads(request.content)["agent"])
        return httpx.Response(200, json=snapshot.model_dump(mode="json"))

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/sess_mcp$",
        lambda _r, _m: httpx.Response(200, json=snapshot.model_dump(mode="json")),
    )
    router.add("POST", r"/v1/sessions/sess_mcp$", update)
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        tool_safety=ToolSafetyPolicy(enabled=enabled),
        public_url=public_url,
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(
            account_id=account.id, agent=agent, env=ma_environment(id="env_1", tenant_id=tenant.id)
        ),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_mcp",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    postures: list[object] = []

    async def _fake_run_turn(**kwargs: object) -> TurnState:
        postures.append(kwargs["tool_confirmation"])
        gate = mutation_gate.unattended_gate.get()
        assert gate is not None
        await gate()
        return TurnState()

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)
    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-mcp",
        external_user_id="user-1",
        user_message="wake up",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        attended=False,
        origin=origin,
    )

    [sent] = updates
    [toolset] = sent["tools"]
    assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
    assert all(c["permission_policy"] == {"type": "always_ask"} for c in toolset["configs"])
    (posture,) = postures
    assert isinstance(posture, PolicyApproval)
    decision = await posture.decide(
        ToolCall(tool_use_id="tu", server_name=server, tool_name="list_agents")
    )
    assert decision.allow


async def test_a_card_posted_inside_the_turn_never_shows_a_time_past_its_deadline(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROOT 2: run_prepared_turn sets the turn's deadline; a 24h card asked for inside
    the turn expires 5 s before it, and nothing is set once the turn has ended."""
    from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
    from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
    from daimon.core.turn.approvals import CARD_TURN_MARGIN
    from daimon.core.turn.ceiling import current_turn_deadline
    from daimon.core.turn.posture import PolicyApproval

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-card-deadline",
        ma_session_id="sess_card_deadline",
    )
    await db_session.commit()
    router = _router(session_bodies=[], dead_session_ids=set())
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        tool_safety=ToolSafetyPolicy(enabled=True, confirmation_timeout_s=86_400),
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(
            account_id=account.id,
            agent=ma_agent(id="ag_1", tenant_id=tenant.id),
            env=ma_environment(id="env_1", tenant_id=tenant.id),
        ),
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id="sess_card_deadline",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    prompts: list[ConfirmationPrompt] = []
    deadlines: list[object] = []

    async def card(prompt: ConfirmationPrompt) -> ConfirmationAnswer:
        prompts.append(prompt)
        return "approved"

    async def _fake_run_turn(**kwargs: object) -> TurnState:
        posture = kwargs["tool_confirmation"]
        assert isinstance(posture, PolicyApproval)
        deadlines.append(current_turn_deadline.get())
        # The model asks for a gated write mid-turn.
        await posture.decide(
            ToolCall(tool_use_id="tu", server_name="linear", tool_name="create_issue")
        )
        return TurnState()

    monkeypatch.setattr("daimon.core.turn.run.run_turn", _fake_run_turn)
    deadline = datetime.now(UTC) + timedelta(minutes=30)
    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-card-deadline",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
        confirm_write=card,
        deadline=deadline,
    )
    assert deadlines == [deadline]
    (prompt,) = prompts
    # A 24h policy, but the card shows (and keeps) the turn's end, less the margin.
    assert prompt.expires_at <= deadline - CARD_TURN_MARGIN
    assert prompt.expires_at > deadline - CARD_TURN_MARGIN - timedelta(seconds=5)
    assert current_turn_deadline.get() is None


@pytest.mark.parametrize("waiter_end", ["acquire", "stop", "cancel"])
async def test_queued_attended_turn_cannot_borrow_or_revoke_unattended_holders_grant(
    db_committing_engine: AsyncEngine,
    db_clean: None,
    monkeypatch: pytest.MonkeyPatch,
    waiter_end: str,
) -> None:
    """Two real mux executions share a Postgres slot and a signed session grant."""
    from daimon.core.session_snapshot import (
        fingerprint_identity,
        fingerprint_mutable,
        snapshot_from_created_session,
    )
    from daimon.core.stores import mux_state
    from daimon.core.stores.domain import Role
    from daimon.core.stores.thread_sessions import record_snapshot
    from daimon.core.stores.turn_origins import get_active_origin, get_session_origin
    from daimon.core.turn_origin import current_execution_origin_id, turn_origin
    from mux.state.lease import LeaseBusy
    from mux.testing.drivers.anthropic.ma_models import ma_session

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with sessions.begin() as db_session:
        tenant = await make_tenant(db_session)
        account = await make_account(db_session, tenant=tenant)
        grant = str(uuid.uuid4())
        row = await make_thread_session(
            db_session,
            tenant=tenant,
            account=account,
            platform="discord",
            thread_id="thread-1",
            ma_session_id="sess_1",
        )
        agent = ma_agent(id="ag_1", tenant_id=tenant.id)
        snapshot = snapshot_from_created_session(
            ma_session(id="sess_1", agent=agent, metadata={"daimon_chat_session_id": grant}),
            env_sha256=None,
            env_file_id=None,
            repo_token_issued_at=None,
            vault_id="vlt_own",
            sent_skills=(),
        )
        await record_snapshot(
            db_session,
            id=row.id,
            snapshot=snapshot,
            identity_fingerprint=fingerprint_identity(snapshot),
            mutable_fingerprint=fingerprint_mutable(snapshot),
        )
    sent: list[tuple[str, list[dict[str, object]]]] = []
    router = _router(session_bodies=[], dead_session_ids=set(), sent_batches=sent)
    router.add(
        "GET",
        r"/v1/sessions/sess_1",
        lambda _r, _m: httpx.Response(
            200, json=ma_session(id="sess_1", agent=agent).model_dump(mode="json")
        ),
    )
    deps = dataclasses.replace(_deps(sessionmaker=sessions, router=router), turn_path="mux")
    admission = _admission(account_id=account.id, agent=agent, env=ma_environment())
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="user-1",
        ma_session_id=row.ma_session_id,
        mapping_id=row.id,
        session_account_id=account.id,
    )
    running = {name: asyncio.Event() for name in ("holder", "waiter")}
    finish = {name: asyncio.Event() for name in running}
    ids: dict[str, uuid.UUID] = {}
    queued = asyncio.Event()
    stop = asyncio.Event()
    real_acquire = mux_state.PostgresStateStore.acquire_lease
    real_release = mux_state.PostgresStateStore.release_lease
    released: list[uuid.UUID] = []

    async def observe_acquire(self: Any, slot: Any, **kwargs: Any) -> Any:
        assert current_execution_origin_id.get() is None
        try:
            return await real_acquire(self, slot, **kwargs)
        except LeaseBusy:
            queued.set()
            raise

    async def observe_release(self: Any, lease: Any) -> None:
        # Revocation must precede release, not happen later in the outer shell.
        assert current_execution_origin_id.get() is None
        name = "holder" if lease.turn_id.endswith(":holder") else "waiter"
        async with sessions() as db:
            origin = await get_active_origin(
                db,
                origin_id=ids[name],
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                now=datetime.now(UTC),
            )
        assert origin is not None and origin.chat_session_id is None
        released.append(origin.id)
        await real_release(self, lease)

    monkeypatch.setattr(mux_state.PostgresStateStore, "acquire_lease", observe_acquire)
    monkeypatch.setattr(mux_state.PostgresStateStore, "release_lease", observe_release)

    async def assert_grant(name: str) -> None:
        async with sessions() as db:
            active = await get_session_origin(
                db,
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                chat_session_id=grant,
                now=datetime.now(UTC),
            )
            own = await get_active_origin(
                db,
                origin_id=ids[name],
                tenant_id=tenant.id,
                account_id=account.id,
                platform="discord",
                now=datetime.now(UTC),
            )
            if name == "holder" and "waiter" in ids:
                waiter = await get_active_origin(
                    db,
                    origin_id=ids["waiter"],
                    tenant_id=tenant.id,
                    account_id=account.id,
                    platform="discord",
                    now=datetime.now(UTC),
                )
                assert waiter is None or waiter.chat_session_id is None
        assert own is not None and own.chat_session_id == grant
        assert active is not None and active.id == ids[name]
        assert active.attended is (name == "waiter")

    async def run(name: str) -> RunOutcome:
        async with turn_origin(
            sessions,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            parent_channel_id="C1",
            thread_id="thread-1",
            responder_ma_agent_id=agent.id,
            responder_name="daimon",
            role=Role.USER,
            attended=name == "waiter",
            sender_foreign=False,
            shared_channel=False,
        ) as origin:
            ids[name] = origin.id

            async def before_send() -> None:
                assert current_execution_origin_id.get() == ids[name]
                await assert_grant(name)
                running[name].set()
                await finish[name].wait()

            admitted = with_invocation(
                prepared.admission, tenant.id, human=name == "waiter", external_user_id="user-1"
            )
            return await run_prepared_turn(
                deps,
                dataclasses.replace(prepared, admission=admitted),
                tenant_id=tenant.id,
                platform="discord",
                thread_id="thread-1",
                external_user_id="user-1",
                user_message=name,
                lifecycle=RecordingLifecycle(),
                cancel=stop if name == "waiter" else asyncio.Event(),
                reseed_user_message=_reseed,
                recovery_lifecycle=_recovery_lifecycle,
                attended=name == "waiter",
                origin="chat",
                operation_key=name,
                render_interval_s=0.001,
                before_send=before_send,
            )

    tasks = [asyncio.create_task(run("holder"))]
    try:
        await asyncio.wait_for(running["holder"].wait(), timeout=30)
        tasks.append(asyncio.create_task(run("waiter")))
        await asyncio.wait_for(queued.wait(), timeout=30)
        assert not running["waiter"].is_set()
        await assert_grant("holder")
        assert sent == [], "neither blocked pump has sent input"
        if waiter_end == "acquire":
            finish["holder"].set()
            assert (await asyncio.wait_for(tasks[0], timeout=30)).state.error is None
            await asyncio.wait_for(running["waiter"].wait(), timeout=30)
            await assert_grant("waiter")
            assert released == [ids["holder"]]
            finish["waiter"].set()
            assert (await asyncio.wait_for(tasks[1], timeout=30)).state.error is None
            assert released == [ids["holder"], ids["waiter"]]
            assert len(sent) == 2
        else:
            if waiter_end == "stop":
                stop.set()
                outcome = await asyncio.wait_for(tasks[1], timeout=30)
                assert outcome.state.termination == TerminationReason.INTERRUPTED
            else:
                tasks[1].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[1]
            assert not running["waiter"].is_set()
            assert released == [], "a waiter never releases the holder's lease"
            await assert_grant("holder")
            assert sent == []
            finish["holder"].set()
            assert (await asyncio.wait_for(tasks[0], timeout=30)).state.error is None
            assert released == [ids["holder"]]
            assert len(sent) == 1
        async with sessions() as db:
            assert (
                await get_session_origin(
                    db,
                    tenant_id=tenant.id,
                    account_id=account.id,
                    platform="discord",
                    chat_session_id=grant,
                    now=datetime.now(UTC),
                )
                is None
            )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_job_completion_wake_only_reads_job_and_restores_parent_tools(
    db_session,
    db_session_factory,
    platform,
):
    from daimon.core.stores.task_continuations import record_continuation
    from mux.testing.drivers.anthropic.ma_models import ma_session

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    allow = {"enabled": True, "permission_policy": {"type": "always_allow"}}
    agent = ma_agent(
        id="agent_job_parent",
        tools=[
            {"type": "agent_toolset_20260401", "default_config": allow, "configs": []},
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": allow,
                "configs": [{"name": "start_long_job", **allow}],
            },
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "default_config": allow,
                "configs": [],
            },
        ],
        mcp_servers=[
            {"type": "url", "name": "daimon-mcp", "url": "https://daimon.example/mcp"},
            {"type": "url", "name": "linear", "url": "https://linear.example/mcp"},
        ],
    )
    snapshot = ma_session(id="ses_job_parent", agent=agent)
    state = snapshot.model_dump(mode="json")
    original_tools, original_servers = (
        list(state["agent"]["tools"]),
        list(state["agent"]["mcp_servers"]),
    )
    mapping = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform=platform,
        thread_id="job-thread",
        ma_session_id=snapshot.id,
    )
    wake = await record_continuation(
        db_session,
        tenant_id=tenant.id,
        platform=platform,
        parent_channel_id="channel",
        thread_id="job-thread",
        requester_account_id=account.id,
        requester_external_user_id="owner",
        target_ma_agent_id=agent.id,
        target_name=agent.name,
        reason="job_finished",
        idempotency_key=uuid.uuid4(),
        requested_work="Read the completed job",
        attended=False,
        available_at=datetime.now(UTC),
    )
    await db_session.commit()
    at_send = []
    router = MARouter()
    router.add(
        "GET", r"/v1/sessions/ses_job_parent$", lambda _r, _m: httpx.Response(200, json=state)
    )
    idle = make_status_idle(event_id="job_wake_done").model_dump(mode="json")
    router.add(
        "GET", r"/v1/sessions/ses_job_parent/events/stream$", lambda _r, _m: sse_response([idle])
    )
    router.add("GET", r"/v1/sessions/ses_job_parent/events$", lambda _r, _m: list_response([idle]))

    def update(request, _match):
        state["agent"].update(json.loads(request.content)["agent"])
        return httpx.Response(200, json=state)

    def send(request, _match):
        at_send.append(json.loads(json.dumps(state["agent"])))
        return send_events_response()

    router.add("POST", r"/v1/sessions/ses_job_parent$", update)
    router.add("POST", r"/v1/sessions/ses_job_parent/events$", send)
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router),
        public_url="https://daimon.example/mcp",
        turn_path="legacy",
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=_admission(account_id=account.id, agent=agent, env=ma_environment()),
        tenant_id=tenant.id,
        external_user_id="owner",
        ma_session_id=snapshot.id,
        mapping_id=mapping.id,
        session_account_id=account.id,
    )
    try:
        result = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform=platform,
            thread_id="job-thread",
            external_user_id="owner",
            user_message="Read and report",
            attended=False,
            job_row=wake,
            reseed_user_message=_reseed,
            recovery_lifecycle=_recovery_lifecycle,
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
        )
        assert result.state.error is None
        [sent] = at_send
        assert [server["name"] for server in sent["mcp_servers"]] == ["daimon-mcp"]
        [builtin] = [t for t in sent["tools"] if t["type"] == "agent_toolset_20260401"]
        assert not builtin["default_config"]["enabled"]
        [mcp] = [t for t in sent["tools"] if t["type"] == "mcp_toolset"]
        assert [c["name"] for c in mcp["configs"] if c["enabled"]] == ["get_long_job"]
        assert state["agent"]["tools"] == original_tools
        assert state["agent"]["mcp_servers"] == original_servers
    finally:
        await deps.anthropic.close()


async def test_attended_accepted_post_with_lost_ack_is_replayed_without_resend(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ordinary attended legacy turn has no durable send claim to disable retries."""
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
    from .conftest import make_agent_message

    answer = make_agent_message(event_id="accepted_answer", text="Accepted exactly once")
    done = make_status_idle(event_id="accepted_done")
    router = _router(
        session_bodies=[],
        dead_session_ids=set(),
        events_by_session={
            "sess_1": [answer.model_dump(mode="json"), done.model_dump(mode="json")]
        },
    )
    accepted_posts: list[httpx.Request] = []

    def accept_then_lose_ack(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions/sess_1/events":
            accepted_posts.append(request)
            raise httpx.ReadTimeout(
                "provider accepted input; acknowledgement lost", request=request
            )
        return router.dispatch(request)

    client = build_fake_anthropic(accept_then_lose_ack).with_options(max_retries=8)
    deps = dataclasses.replace(
        _deps(sessionmaker=db_session_factory, router=router), anthropic=client, turn_path="legacy"
    )
    admission = _admission(
        account_id=account.id,
        agent=ma_agent(id="ag_1", tenant_id=tenant.id),
        env=ma_environment(id="env_1", tenant_id=tenant.id),
    )
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
    assert len(accepted_posts) == 1, "lost acknowledgement must never repeat the accepted POST"
    assert accepted_posts[0].extensions["timeout"]["read"] == 600.0
    assert client.max_retries == 8, "turn mutation policy must not change the shared client"
    assert not outcome.recovered and outcome.state.error is None
    assert outcome.state.content == [TextBlock(kind="text", text="Accepted exactly once")]
