"""Actual SDK/host/wake/database proofs for explicitly installed OpenAI hooks."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, NamedTuple, cast

import httpx
import pytest
from daimon.core import channel_backend
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.mux_backend import TurnBackendRequest
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn import driver, openai_host
from daimon.core.turn.admission import admit_impl
from daimon.core.turn.openai_host import OpenAIHostRuntime, prepare_openai
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest, bind_session_impl
from daimon.core.turn.termination import TerminationReason
from daimon.core.turn_hold import RequesterOrg
from daimon.testing import pytest_plugin as database_plugin
from daimon.testing.accounting_conformance import HostAccountingHook
from daimon.testing.db import DatabasePreflight, require_owned_sessionmaker
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_tenant_config,
)
from daimon.testing.openai_conformance import (
    C12_EXCLUSION,
    OpenAIHostTransport,
    compose_openai_host,
)
from daimon.testing.turn_conformance import BackendSelectionEnvelope, WakeEnvelope
from daimon.testing.turn_fakes import RecordingLifecycle
from daimon.testing.turn_router import build_turn_router
from mux.contracts.config import BackendConfig
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ModelRef, ResourceRef, Revision, ThreadRef
from mux.contracts.resources import ProviderBinding, SessionSpec
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.errors import ScopeViolation
from mux.state.lease import Slot
from mux.testing.conformance.runner import Adapter, PendingKind, Registry, run_fixture
from mux.testing.drivers.anthropic.ma import json_body, make_fake_memory_store_handler
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.openai.conformance import OpenAIScriptedTransport, register
from mux.testing.isolated_sdk import SDK_ENV_VARS, build_async_anthropic, build_async_openai
from sqlalchemy import event as sql_event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .test_openai_host import SESSION_ID, Wire
from .test_provider_dispatch import (
    ACCOUNT,
    PROFILE,
    REVISION,
    SCOPE,
    TENANT,
    admission,
    deps_for_pinned_mux,
)

NEW_SESSION = "replacement-openai-session"


class NativeStream(httpx.AsyncByteStream):
    def __init__(self, wire: NativeWire) -> None:
        self.wire = wire
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await self.wire.sent.wait()
        root = self.wire.turns[-1]
        for event in (
            {
                "type": "agent.session.turn.in_progress",
                "event_id": "turn-1:running",
                "turn": {**root, "status": "in_progress"},
            },
            {
                "type": "agent.session.turn.item.done",
                "event_id": "turn-1:answer",
                "turn_id": "turn-1",
                "item": self.wire.items[-1],
            },
            {
                "type": "agent.session.turn.completed",
                "event_id": "turn-1:done",
                "turn": {**root, "status": "completed"},
            },
        ):
            event["session_id"] = self.wire.session_id
            yield ("data: " + json.dumps(event) + "\n\n").encode()
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class NativeWire(Wire):
    """Two provisioned native IDs; only native replies change, never host facts."""

    turns: list[dict[str, Any]]
    items: list[dict[str, Any]]

    def __init__(self) -> None:
        super().__init__()
        self.session_id = SESSION_ID
        self.expected_spend_limit = None
        self.native_streams: list[NativeStream] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/events"):
            self.requests.append(req)
            assert req.url.path == f"/v1/agents/sessions/{self.session_id}/events"
            assert req.headers["OpenAI-Beta"] == "agents=v1"
            assert req.headers["Accept"] == "text/event-stream"
            assert req.url.params["stream"] == "true"
            self.sent = asyncio.Event()
            source = NativeStream(self)
            self.native_streams.append(source)
            return httpx.Response(200, stream=source, headers={"Content-Type": "text/event-stream"})
        return super().handle(req)


@pytest.fixture(autouse=True)
def clean_sdk_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SDK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class Host(NamedTuple):
    request: ProviderPreparationRequest
    first: PreparedTurn
    binding: ProviderBinding
    store: PostgresStateStore
    wire: NativeWire
    runtime: OpenAIHostRuntime


db_preflight = database_plugin.db_preflight


@pytest.fixture(scope="session", autouse=True)
def owned_database_preflight(db_preflight: DatabasePreflight) -> None:
    """Use the shared URL/schema admission before any engine fixture."""


@pytest.fixture
async def native_host(
    db_session_factory: async_sessionmaker[AsyncSession], db_clean: None
) -> AsyncIterator[Host]:
    require_owned_sessionmaker(db_session_factory)
    async with db_session_factory.begin() as db:
        await make_tenant(db, id=TENANT)
    store = PostgresStateStore(db_session_factory)
    await store.put_config_revision(REVISION)
    wire = NativeWire()

    async def plan(request: ProviderPreparationRequest) -> SessionSpec:
        return SessionSpec(
            agent=ResourceRef(
                id="native-agent",
                kind="agent",
                provider="openai",
                account_scope_id="project",
                tenant_id=SCOPE.tenant_id,
                account_id=SCOPE.account_id,
            ),
            agent_revision=Revision(local=0),
            config_revision=REVISION.local,
        )

    async with build_async_openai(
        transport=httpx.MockTransport(wire.handle),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        runtime = OpenAIHostRuntime(
            lambda revision, scope: SDKTransport(sdk),
            OpenAIRecoveryJournal(),
            OpenAIUsageRevisions(),
            account_scope_id="project",
            session_plan=plan,
            authorization=lambda scope, kind, id_: scope == SCOPE,
            controls=SessionControls(
                model="gpt-6-luna", multi_agent_enabled=False, container_size="small"
            ),
        )
        deps = replace(
            deps_for_pinned_mux(None, db_session_factory),
            channel_backends=True,
            state_store=store,
            turn_runtimes={PROFILE: runtime},
        )
        request = ProviderPreparationRequest(
            deps,
            admission(),
            SCOPE,
            TENANT,
            "slack",
            "caller",
            "thread",
            ACCOUNT,
            True,
            DEFAULT_MA_CAPABILITIES,
            None,
            datetime.now(UTC) + timedelta(seconds=30),
            lambda: datetime.now(UTC),
        )
        first = await prepare_openai(request)
        assert first.session_ref is not None
        binding = await store.get_binding(
            Slot(
                thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
                account_id=SCOPE.account_id,
            )
        )
        assert binding is not None
        yield Host(request, first, binding, store, wire, runtime)


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize("exact_ci", [False, True])
async def test_injected_contributor_bind_refuses_before_database_or_sdk_io(
    monkeypatch: pytest.MonkeyPatch,
    routed: bool,
    exact_ci: bool,
) -> None:
    from daimon.core._models import Tenant

    for flag in ("GITHUB_ACTIONS", "CI"):
        monkeypatch.setenv(flag, "true" if exact_ci else "false")
    contributor = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test")
    owned = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_n11")
    connected: list[object] = []

    def refuse_connection(*args: object, **kwargs: object) -> None:
        connected.append(None)
        raise AssertionError("contributor fixture attempted a connection")

    for engine in (contributor, owned):
        sql_event.listen(engine.sync_engine, "do_connect", refuse_connection)
    sessions = (
        async_sessionmaker(owned, binds={Tenant: contributor})
        if routed
        else async_sessionmaker(contributor)
    )
    fixture = cast(
        Callable[[async_sessionmaker[AsyncSession], None], AsyncIterator[Host]],
        inspect.unwrap(native_host),
    )
    try:
        # Direct unwrapping deliberately bypasses configured-URL preflight.
        # An injected primary/routed bind must refuse, never broadly skip.
        with pytest.raises(ValueError, match="owned local lane"):
            await anext(fixture(sessions, None))
        assert connected == []
    finally:
        await contributor.dispose()
        await owned.dispose()


@pytest.mark.parametrize("declared", [False, True])
async def test_absent_envelopes_preserve_pending_and_native_transport(declared: bool) -> None:
    registry = Registry()
    async with register(registry, sdk_factory=build_async_openai):
        original = registry.create("openai.offline")
        if not declared:
            original = replace(original, pending={})
        composed = compose_openai_host(original)
        assert composed.pending["C12"] == C12_EXCLUSION
        for fid in ("C14", "C17", "C18"):
            assert composed.pending[fid].kind == PendingKind.ADAPTER_DEPENDENCY
            if declared:
                assert composed.pending[fid] == original.pending[fid]
        assert composed.driver is original.driver and composed.store is original.store
        assert composed.transport.mutation_count == 0
        results = [await run_fixture(fid, composed) for fid in ("C12", "C14", "C17", "C18")]
        assert all(row.status == "pending" for row in results)
        assert composed.transport.mutation_count == 0


@pytest.mark.parametrize("mismatch", [False, True])
async def test_c17_actual_sdk_host_codec_durable_wake_and_journal(
    native_host: Host, mismatch: bool
) -> None:
    request, first, before, store, wire, runtime = native_host
    wire.session_id = NEW_SESSION
    native = await openai_host.driver(runtime, REVISION, SCOPE).sessions.create(
        SCOPE, await runtime.session_plan(request), key="openai:prepare:replacement-fixture"
    )
    assert native.ref.id == NEW_SESSION and native.ref != first.session_ref
    replacement = before.model_copy(
        update={
            "generation": before.generation + 1,
            "native_refs": dict(before.native_refs) | {"session": native.ref.id},
        }
    )
    expected = Event(
        id="turn-1:done",
        session_id=native.ref.id,
        sequence=0,
        type="session.turn_ended",
        authority="record",
        observed_at=datetime.now(UTC),
        turn_id="turn-1",
        payload={
            "root_turn_id": "turn-1",
            "outcome": "completed",
            "native_reason": "wrong" if mismatch else "agent.session.turn.completed",
            "cancel_receipt": None,
        },
        native=NativeProvenance(provider="openai", api_revision="agents=v1"),
    )

    async def run_host(persistence: TurnPersistence) -> None:
        result = await driver.run_turn(
            anthropic=request.deps.anthropic,
            session_id=native.ref.id,
            user_message="wake",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="headless-unrecorded"),
            path="mux",
            scope=SCOPE,
            session_ref=native.ref,
            profile=PROFILE,
            render_interval_s=3600,
            backend_request=TurnBackendRequest(
                PROFILE,
                request.deps.anthropic,
                SCOPE,
                native.ref.id,
                deps=request.deps,
                config=REVISION,
                session=native.ref,
                runtime=runtime,
            ),
        )
        assert result.termination == TerminationReason.COMPLETED and result.error is None

    continuation = ContinuationRequest(
        tenant_id=TENANT,
        platform="slack",
        parent_channel_id="channel",
        thread_id="thread",
        requester_account_id=ACCOUNT,
        requester_external_user_id="caller",
        target_ma_agent_id="native-agent",
        target_name="offline",
        requested_work="wake",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    envelope = WakeEnvelope(
        request.deps.sessionmaker,
        store,
        SCOPE,
        continuation,
        replacement,
        native.ref,
        expected,
        datetime.now(UTC),
        run_host,
    )
    adapter = Adapter(
        openai_host.driver(runtime, REVISION, SCOPE), store, OpenAIScriptedTransport()
    )
    composed = compose_openai_host(adapter, wake=envelope)
    assert "C17" not in composed.pending
    hook = cast(OpenAIHostTransport, composed.transport).host_wake_evidence
    assert hook is not None
    facts = await hook()
    assert facts["before_generation"] == 1
    assert facts["current_generation"] == facts["replacement_generation"] == 2
    assert facts["stale_binding_refused"] is True and facts["stale_pump_calls"] == 0
    assert facts["stale_wake_started"] is facts["stale_wake_settled"] is False
    assert facts["wake_started"] is facts["wake_settled"] is facts["lease_released"] is True
    assert facts["wake_status"] == "delivered" and facts["pump_calls"] == 1
    assert facts["journal_event_present"] is (not mismatch)
    input_posts = [
        r for r in wire.requests if r.method == "POST" and r.url.path.endswith("/events")
    ]
    assert len(input_posts) == 1 and NEW_SESSION in input_posts[0].url.path
    assert all(r.url.host == "openai.invalid" for r in wire.requests)
    assert all(s.closed for s in wire.native_streams)
    with pytest.raises(ValueError, match="next generation"):
        await hook()
    with pytest.raises(ScopeViolation):
        foreign = compose_openai_host(
            adapter,
            wake=replace(envelope, scope=SCOPE.model_copy(update={"account_id": "foreign"})),
        )
        foreign_hook = cast(OpenAIHostTransport, foreign.transport).host_wake_evidence
        assert foreign_hook is not None
        await foreign_hook()


async def test_c12_queried_partial_facts_never_clear_overlap_exclusion(
    native_host: Host, db_engine: AsyncEngine
) -> None:
    _, first, binding, store, _, runtime = native_host
    assert first.session_ref is not None
    hook = HostAccountingHook(
        db_engine,
        SCOPE,
        binding,
        first.session_ref,
        ModelRef(provider="openai", id="gpt-6-luna"),
        datetime.now(UTC),
        "openai-host-envelope",
    )
    adapter = Adapter(
        openai_host.driver(runtime, REVISION, SCOPE), store, OpenAIScriptedTransport()
    )
    composed = compose_openai_host(adapter, accounting=hook)
    assert composed.pending["C12"] == C12_EXCLUSION
    facts = await hook()
    assert facts["aggregate_grains"] == ()
    assert facts["aggregate_rejections"]
    assert facts["pending_reason"] == hook.pending_reason


async def test_c14_normal_current_config_retains_native_binding(
    native_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _, store, wire, runtime = native_host
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.core.stores.tenants import get_tenant

    async with request.deps.sessionmaker.begin() as db:
        tenant = await get_tenant(db, TENANT)
        assert tenant is not None
        account = await make_account(db, tenant=tenant, id=ACCOUNT)
        await make_platform_principal(
            db, platform="slack", external_id="caller", tenant=tenant, account=account
        )
        await make_tenant_config(
            db, tenant=tenant, agent_name="test-agent", environment_name="test-env"
        )
        await make_ledger_entry(db, tenant=tenant, delta_usd=Decimal(100))
        await set_access_policy(
            db,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(enabled_backends=("anthropic", "openai")),
        )
    ancillary = build_turn_router(str(TENANT))
    memory = make_fake_memory_store_handler()
    calls: list[httpx.Request] = []
    created: list[str] = []

    def create(req: httpx.Request, match: re.Match[str]) -> httpx.Response:
        body = json_body(req)
        id_ = f"ancillary-session-{len(created) + 1}"
        created.append(id_)
        return httpx.Response(
            200,
            json=ma_session(
                id=id_,
                agent_id="ag_parity_test",
                environment_id="env_parity_test",
                metadata=body.get("metadata"),
                resources=body.get("resources", ()),
            ).model_dump(mode="json"),
        )

    ancillary.add("POST", r"/v1/sessions", create)

    def handle(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.startswith("/v1/memory_stores"):
            return memory(req)
        return ancillary.dispatch(req)

    async with build_async_anthropic(
        transport=httpx.MockTransport(handle),
        api_key="offline",
        base_url="https://anthropic.invalid",
    ) as client:
        deps = replace(request.deps, anthropic=client)
        admitted_revisions: list[tuple[str, int]] = []

        async def prepare(thread: str) -> PreparedTurn:
            admitted = await admit_impl(
                deps,
                origin="chat",
                tenant_id=TENANT,
                requester_org=RequesterOrg.HOST,
                platform="slack",
                external_user_id="caller",
                channel_id="channel",
                thread_id=thread,
                now=datetime.now(UTC),
            )
            assert admitted.backend_revision is not None
            admitted_revisions.append(
                (admitted.backend_revision.profile, admitted.backend_revision.local)
            )
            return await bind_session_impl(
                deps,
                admitted,
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                thread_id=thread,
                session_account_id=ACCOUNT,
                reuse_existing=True,
            )

        async def first() -> PreparedTurn:
            return await prepare("thread")

        async def new() -> PreparedTurn:
            return await prepare("new-thread")

        async def change() -> None:
            async with deps.sessionmaker.begin() as db:
                await channel_backend.set_channel_backend(db, REVISION.channel, BackendConfig())

        async def without_original() -> PreparedTurn:
            with monkeypatch.context() as isolated:
                isolated.setattr(
                    channel_backend,
                    "RUNNABLE_PROFILES",
                    channel_backend.RUNNABLE_PROFILES - {PROFILE},
                )
                return await first()

        def mutations() -> int:
            return sum(r.method in ("POST", "PATCH", "DELETE") for r in wire.requests + calls)

        selection = BackendSelectionEnvelope(
            store,
            SCOPE,
            REVISION.channel,
            "thread",
            "new-thread",
            first,
            change,
            first,
            new,
            without_original,
            mutations,
        )
        adapter = Adapter(
            openai_host.driver(runtime, REVISION, SCOPE), store, OpenAIScriptedTransport()
        )
        composed = compose_openai_host(adapter, selection=selection)
        hook = cast(OpenAIHostTransport, composed.transport).host_backend_selection_evidence
        assert hook is not None and "C14" not in composed.pending
        facts = await hook()
    # Actual normal admission selects the retained native binding, despite the
    # changed channel configuration; no callback pins an old revision itself.
    assert admitted_revisions == [
        (PROFILE, 1),
        (PROFILE, 1),
        ("anthropic.managed_agents", 2),
    ]
    assert (
        facts["first"] == facts["first_binding"] == facts["existing"] == facts["existing_binding"]
    )
    assert facts["first"] == (PROFILE, 1, SESSION_ID, "openai")
    # The default Anthropic path retains its existing legacy binding storage;
    # it does not invent a native mux binding for this new thread.
    assert facts["new_binding"] is None
    assert facts["new_thread"] == ("anthropic.managed_agents", 2, created[0], "anthropic")
    assert created == ["ancillary-session-1"] and created[0] != SESSION_ID
    assert facts["missing_original_refusal"] == "backend_unsupported"
    assert facts["missing_original_mutations"] == 0
    assert "finding" not in facts
    assert facts["original_config"] == (PROFILE, 1)
    assert facts["changed_config"] == ("anthropic.managed_agents", 2)
    assert all(r.url.host == "anthropic.invalid" for r in calls)
    assert all(r.url.host == "openai.invalid" for r in wire.requests)
