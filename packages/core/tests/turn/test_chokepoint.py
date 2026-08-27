"""End-to-end core chokepoint test (SPEC "Acceptance Criteria"): drives
`admit()` -> `bind_session()` -> `run_prepared_turn()` in sequence, exactly
as an adapter will, against a fake MA transport and real Postgres -- proving
the complete D-01 chokepoint before any adapter touches it.

Adapter-free by construction: this module lives in `packages/core/tests` and
must never import `daimon.adapters.*`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core.config import McpSettings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import PreparedTurn, bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.testing.ma import (
    EMPTY_CLOUD_CONFIG,
    MARouter,
    build_fake_anthropic,
    list_response,
    make_fake_memory_store_handler,
    send_events_response,
    sse_response,
)
from daimon.testing.turn_fakes import RecordingLifecycle
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import make_status_idle

from daimon.testing.factories import (  # isort: skip
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
)

_NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _agent(*, agent_id: str, name: str, tenant_id: uuid.UUID) -> BetaManagedAgentsAgent:
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


def _env(*, env_id: str, name: str, tenant_id: uuid.UUID) -> BetaEnvironment:
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


def _chokepoint_router(
    *, tenant_id: uuid.UUID, session_bodies: list[dict[str, object]]
) -> MARouter:
    """Resolve agent/environment for tenant_id, serve memory-store cold
    provisioning + session-create (each call assigns the next `sess_N` id),
    events.send, and events.stream (one span.model_request_end + terminal
    idle event for every session)."""
    agent = _agent(agent_id="ag_1", name="daimon", tenant_id=tenant_id)
    env = _env(env_id="env_1", name="default", tenant_id=tenant_id)

    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, _m: list_response([agent.model_dump(mode="json")]))
    router.add(
        "GET",
        r"/v1/agents/ag_1",
        lambda req, _m: httpx.Response(200, json=agent.model_dump(mode="json")),
    )
    router.add(
        "GET", r"/v1/environments", lambda req, _m: list_response([env.model_dump(mode="json")])
    )
    router.add(
        "GET",
        r"/v1/environments/env_1",
        lambda req, _m: httpx.Response(200, json=env.model_dump(mode="json")),
    )

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

    def _stream(_request: httpx.Request, _match: object) -> httpx.Response:
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


def _deps(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    defaults_root: Path,
    router: MARouter,
) -> TurnDeps:
    return TurnDeps(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessionmaker,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=defaults_root,
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1.0"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )


async def _reseed() -> str:
    return "full history reseed"


def _recovery_lifecycle(_cancel: asyncio.Event) -> RecordingLifecycle:
    return RecordingLifecycle()


async def test_chokepoint_admit_bind_session_run_prepared_turn_end_to_end(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await db_session.commit()

    session_bodies: list[dict[str, object]] = []
    router = _chokepoint_router(tenant_id=tenant.id, session_bodies=session_bodies)
    deps = _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router)

    # --- Stage one: admit() ---
    admission = await admit(
        deps,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        channel_id="chan-1",
        now=_NOW,
    )
    assert isinstance(admission, Admission), "admit() must return a real Admission"

    # --- (simulated platform step): resolve the caller's session_account_id ---
    session_account_id = admission.account_id

    # --- Stage two: bind_session() ---
    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="user-1",
        thread_id="thread-e2e",
        session_account_id=session_account_id,
        reuse_existing=True,
    )
    assert isinstance(prepared, PreparedTurn), "bind_session() must return a real PreparedTurn"

    # --- Stage three: run_prepared_turn() ---
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-e2e",
        external_user_id="user-1",
        user_message="hello",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )

    assert outcome.state.error is None, "the full chokepoint run must complete cleanly"
    assert outcome.recovered is False, "no dead-session signature in this flow"

    async with db_session_factory() as s:
        live = await get_live_thread_session(
            s,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-e2e",
            account_id=session_account_id,
        )
    assert live is not None, "a live thread_sessions row must exist after the full chokepoint"
    assert live.ma_session_id == outcome.ma_session_id, (
        "the live row's session id must match the turn's final session id"
    )

    async with db_session_factory() as s:
        events_rows = await usage_events.list_for_tenant(s, tenant_id=tenant.id)
    assert len(events_rows) == 1, "the span.model_request_end event must write one usage_events row"
    assert events_rows[0].managed_session_id == outcome.ma_session_id, (
        "the recorded managed_session_id must equal the turn's session id"
    )
    assert events_rows[0].model == admission.agent.model.id, (
        "the recorded model must equal the resolved agent's model id"
    )

    async with db_session_factory() as s:
        ledger_rows = await tenant_ledger.list_for_tenant(s, tenant_id=tenant.id)
    debit_rows = [r for r in ledger_rows if r.reason == "turn_debit"]
    assert len(debit_rows) == 1, "record_turn_usage must write exactly one turn_debit ledger row"
    assert debit_rows[0].delta_usd < 0, "a debit row must be negative"


async def test_bind_session_requires_an_admission_value(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Type-level chokepoint claim: bind_session cannot be called without a
    real Admission -- proven here as a runtime TypeError (the pyright half
    of this claim is covered by the project-wide strict gate)."""
    deps = _deps(
        sessionmaker=db_session_factory,
        defaults_root=Path("/nonexistent"),
        router=MARouter(),
    )
    with pytest.raises(TypeError):
        await bind_session(
            deps,
            cast(Admission, "not-an-admission"),
            tenant_id=uuid.uuid4(),
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=uuid.uuid4(),
            reuse_existing=True,
        )
