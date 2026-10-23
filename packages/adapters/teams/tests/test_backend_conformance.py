"""Actual SDK/DB controls: never replace admission, binding or turn execution."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
import textwrap
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.teams import app as platform_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import load_turn_settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger
from daimon.core.stores.domain import ThreadSessionRow
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.turn.admission import AdmissionDenied
from daimon.core.turn.prepare import PreparedTurn
from daimon.testing.adapters.teams.backend_conformance import (
    BackendTurnEnvelope,
    backend_selection_turn,
)
from daimon.testing.factories import make_tenant
from daimon.testing.turn_router import build_turn_router
from mux.contracts.config import BackendConfig, ConfigRevision
from mux.testing.drivers.anthropic.ma import json_body, make_fake_memory_store_handler
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.isolated_sdk import (
    SDK_ENV_VARS,
    AmbientSDKConfigurationError,
    build_async_anthropic,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    THREAD_ID,
    FakeSender,
    bot_token,
    build_teams_runtime,
)

WORKSPACE, CHANNEL, USER, THREAD = ENTRA_TENANT_ID, CHANNEL_ID, AAD_OBJECT_ID, THREAD_ID
OTHER_USER, NEW_THREAD = OTHER_AAD_OBJECT_ID, f"{CHANNEL_ID};messageid=1700000000002"


class Wire:
    def __init__(self, tenant_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.requests: list[httpx.Request] = []
        self.created: list[str] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.stream_hits: list[str] = []
        self.router = build_turn_router(
            str(tenant_id), fresh_event_ids=True, stream_hits=self.stream_hits
        )
        self.memory = make_fake_memory_store_handler()
        self.router.add("POST", r"/v1/sessions", self.create)
        self.router.add(
            "GET",
            r"/v1/sessions/(?P<session_id>[^/]+)",
            lambda _r, m: httpx.Response(200, json=self.sessions[m["session_id"]]),
        )

    def create(self, request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        body = json_body(request)
        native = f"sess_backend_{len(self.created) + 1}"
        self.created.append(native)
        response = ma_session(
            id=native,
            agent_id="ag_parity_test",
            environment_id="env_parity_test",
            metadata=body.get("metadata"),
            resources=body.get("resources", ()),
        ).model_dump(mode="json")
        self.sessions[native] = response
        return httpx.Response(200, json=response)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.startswith("/v1/memory_stores"):
            return self.memory(request)
        return self.router.dispatch(request)


def build_client(wire: Wire) -> AsyncAnthropic:
    """Keep the original test wire policy while refusing ambient SDK settings."""
    return build_async_anthropic(
        transport=httpx.MockTransport(wire.handle),
        api_key="test",
        base_url="https://api.anthropic.com",
        max_retries=2,
        timeout=httpx.Timeout(600.0, connect=5.0),
    )


async def setup(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> tuple[TeamsApp, Callable[[str, str], Awaitable[PreparedTurn]], Wire]:
    import itertools

    ids = itertools.count(100)
    tenant = await make_tenant(db_session, platform="teams", workspace_id=WORKSPACE)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal(100),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await set_channel_backend(db_session, channel_ref(tenant.id, "teams", CHANNEL), BackendConfig())
    await db_session.commit()
    wire = Wire(tenant.id)
    client = build_client(wire)
    default = DeploymentDefault(agent_name="test-agent", environment_name="test-env")
    runtime = build_teams_runtime(db_session_factory, anthropic=client, deployment_default=default)
    runtime = dataclasses.replace(
        runtime, turn_deps=dataclasses.replace(runtime.turn_deps, channel_backends=True)
    )
    app = TeamsApp(runtime=runtime, sender=FakeSender(), commands={}, bot_token=bot_token)

    async def dispatch(thread: str, user: str) -> PreparedTurn:
        inbound = TeamsInbound(
            kind="channel",
            entra_tenant_id=WORKSPACE,
            user_id=user,
            conversation_id=thread,
            channel_id=CHANNEL,
            activity_id=str(next(ids)),
            text="hello",
            service_url=SERVICE_URL,
        )
        return await backend_selection_turn(
            BackendTurnEnvelope(app, inbound, tenant.id, CHANNEL, thread, user)
        )

    return app, dispatch, wire


async def persisted(
    db_session_factory: async_sessionmaker[AsyncSession], prepared: PreparedTurn, thread: str
) -> ThreadSessionRow:
    async with db_session_factory() as session:
        row = await get_live_thread_session(
            session,
            tenant_id=tenant_id_from(prepared),
            platform="teams",
            thread_id=thread,
            account_id=prepared.session_account_id,
        )
    assert row is not None
    assert row.id == prepared.mapping_id
    assert row.ma_session_id == prepared.ma_session_id
    return row


def revision_of(prepared: PreparedTurn) -> ConfigRevision:
    revision = prepared.admission.backend_revision
    assert revision is not None
    return revision


def tenant_id_from(prepared: PreparedTurn) -> uuid.UUID:
    return uuid.UUID(revision_of(prepared).channel.tenant_id)


async def test_real_adapter_binding_reuses_thread_and_isolates_new_thread_and_caller(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, dispatch, wire = await setup(db_session, db_session_factory)
    original_admit, original_bind = platform_module.admit, platform_module.bind_session  # pyright: ignore[reportPrivateImportUsage]
    try:
        first = await dispatch(THREAD, USER)
        existing = await dispatch(THREAD, USER)
        new = await dispatch(NEW_THREAD, USER)
        other = await dispatch(THREAD, OTHER_USER)
        assert first.ma_session_id == existing.ma_session_id
        assert existing.reused
        assert not first.reused and not new.reused and not other.reused
        assert len({first.ma_session_id, new.ma_session_id, other.ma_session_id}) == 3
        assert first.session_account_id == new.session_account_id != other.session_account_id
        for prepared, thread in ((existing, THREAD), (new, NEW_THREAD), (other, THREAD)):
            await persisted(db_session_factory, prepared, thread)
            assert revision_of(prepared).profile == "anthropic.managed_agents"
        assert wire.stream_hits == [
            first.ma_session_id,
            first.ma_session_id,
            new.ma_session_id,
            other.ma_session_id,
        ]
        assert wire.created == [first.ma_session_id, new.ma_session_id, other.ma_session_id]
        assert (
            platform_module.admit is original_admit  # pyright: ignore[reportPrivateImportUsage]
            and platform_module.bind_session is original_bind  # pyright: ignore[reportPrivateImportUsage]
        )
    finally:
        await app.runtime.anthropic.close()


async def test_changed_current_config_refusal_remains_visible_without_native_writes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, dispatch, wire = await setup(db_session, db_session_factory)
    original_admit, original_bind = platform_module.admit, platform_module.bind_session  # pyright: ignore[reportPrivateImportUsage]
    try:
        first = await dispatch(THREAD, USER)
        async with db_session_factory() as session:
            changed = await set_channel_backend(
                session,
                channel_ref(tenant_id_from(first), "teams", CHANNEL),
                BackendConfig(
                    backend="gemini", profile="gemini.inline_reuse", model="gemini-3-flash-preview"
                ),
            )
            await session.commit()
        assert changed.local == revision_of(first).local + 1
        # The mux turn path pins a thread to the revision it started on
        # (`turn/admission.py` retained_revision): a channel change reaches new
        # threads only, so the existing thread keeps running on its own revision.
        # The legacy path has no per-thread pin and refuses every thread.
        on_mux = (app.runtime.turn_deps.turn_path or load_turn_settings().path) == "mux"
        if on_mux:
            retained = await dispatch(THREAD, USER)
            assert retained.reused and retained.ma_session_id == first.ma_session_id
            assert revision_of(retained).local == revision_of(first).local
            assert revision_of(retained).profile == "anthropic.managed_agents"
        refused_threads = (NEW_THREAD,) if on_mux else (THREAD, NEW_THREAD)
        for thread in refused_threads:
            before = len(wire.requests)
            with pytest.raises(AdmissionDenied) as denied:
                await dispatch(thread, USER)
            assert denied.value.reason == "backend_unsupported"
            # Admission may resolve MA tags; missing runtime cannot CREATE/SEND a native session.
            assert not any(request.method != "GET" for request in wire.requests[before:])
        assert wire.created == [first.ma_session_id]
        await persisted(db_session_factory, first, THREAD)
        assert (
            platform_module.admit is original_admit  # pyright: ignore[reportPrivateImportUsage]
            and platform_module.bind_session is original_bind  # pyright: ignore[reportPrivateImportUsage]
        )
    finally:
        await app.runtime.anthropic.close()


async def test_mutation_of_production_reuse_breaks_the_binding_oracle(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, dispatch, wire = await setup(db_session, db_session_factory)
    method = type(app)._bind_and_run  # pyright: ignore[reportPrivateUsage]
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    mutations = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "bind_session"
        ):
            for keyword in node.keywords:
                if keyword.arg == "reuse_existing":
                    keyword.value = ast.Constant(False)
                    mutations += 1
    assert mutations == 1, "control must modify exactly the real production bind call"
    ast.fix_missing_locations(tree)
    namespace: dict[str, Any] = {}
    exec(
        compile(tree, str(Path(inspect.getfile(method))), "exec"),
        platform_module.__dict__,
        namespace,
    )
    try:
        first = await dispatch(THREAD, USER)
        with patch.object(type(app), "_bind_and_run", namespace["_bind_and_run"]):
            broken = await dispatch(THREAD, USER)
        with pytest.raises(AssertionError):
            assert broken.ma_session_id == first.ma_session_id and broken.reused
        assert wire.created == [first.ma_session_id, broken.ma_session_id]
        assert first.ma_session_id != broken.ma_session_id
        await persisted(db_session_factory, broken, THREAD)
    finally:
        await app.runtime.anthropic.close()


async def test_timeout_restores_observers_and_does_not_certify_a_binding(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, _, wire = await setup(db_session, db_session_factory)
    tenant = wire.tenant_id
    inbound = TeamsInbound(
        kind="channel",
        entra_tenant_id=WORKSPACE,
        user_id=USER,
        conversation_id=THREAD,
        channel_id=CHANNEL,
        activity_id="timeout-input",
        text="hello",
        service_url=SERVICE_URL,
    )
    envelope = BackendTurnEnvelope(app, inbound, tenant, CHANNEL, THREAD, USER, timeout_s=1e-9)

    original_admit = platform_module.admit  # pyright: ignore[reportPrivateImportUsage]
    original_bind = platform_module.bind_session  # pyright: ignore[reportPrivateImportUsage]
    try:
        with pytest.raises(TimeoutError):
            await backend_selection_turn(envelope)
        assert platform_module.admit is original_admit  # pyright: ignore[reportPrivateImportUsage]
        assert platform_module.bind_session is original_bind  # pyright: ignore[reportPrivateImportUsage]
        assert wire.created == []
        with pytest.raises(ValueError, match="finite timeout"):
            await backend_selection_turn(dataclasses.replace(envelope, timeout_s=float("inf")))
        assert wire.requests == []
    finally:
        await app.runtime.anthropic.close()


@pytest.mark.parametrize(
    "variable", [name for name in SDK_ENV_VARS if name.startswith("ANTHROPIC_")]
)
def test_fixture_client_refuses_initial_ambient_sdk_settings(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = Wire(uuid.uuid4())
    monkeypatch.setenv(variable, "n7-invented-setting")
    with pytest.raises(AmbientSDKConfigurationError, match=variable) as caught:
        build_client(wire)
    assert "n7-invented-setting" not in str(caught.value)
    assert wire.requests == []


@pytest.mark.parametrize(
    "variable", [name for name in SDK_ENV_VARS if name.startswith("ANTHROPIC_")]
)
@pytest.mark.parametrize("method", ["copy", "with_options"])
async def test_fixture_client_refuses_late_ambient_sdk_settings_on_copy(
    variable: str, method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = Wire(uuid.uuid4())
    client = build_client(wire)
    try:
        monkeypatch.setenv(variable, "n7-invented-setting")
        with pytest.raises(AmbientSDKConfigurationError, match=variable):
            if method == "copy":
                client.copy(default_headers={"X-N7-Explicit": "copy"})
            else:
                client.with_options(default_headers={"X-N7-Explicit": "copy"})
        assert wire.requests == []
    finally:
        await client.close()


async def test_fixture_client_preserves_actual_sdk_wire_and_copy_options() -> None:
    wire = Wire(uuid.uuid4())
    session = "sess_sdk_isolation"
    wire.sessions[session] = ma_session(id=session).model_dump(mode="json")
    client = build_client(wire)
    copied = client.with_options(max_retries=7, default_headers={"X-N7-Explicit": "copy"})
    try:
        assert client.max_retries == 2
        assert client.timeout == httpx.Timeout(600.0, connect=5.0)
        assert copied.max_retries == 7
        assert copied.timeout == client.timeout
        first = await client.get(f"/v1/sessions/{session}", cast_to=object)
        second = await copied.get(f"/v1/sessions/{session}", cast_to=object)
        assert first == second == wire.sessions[session]
        assert len(wire.requests) == 2
        for request in wire.requests:
            assert request.method == "GET"
            assert str(request.url) == f"https://api.anthropic.com/v1/sessions/{session}"
            assert request.headers["x-api-key"] == "test"
            assert request.headers["user-agent"].startswith("AsyncAnthropic/Python ")
            assert "_Isolated" not in request.headers["user-agent"]
            assert request.content == b""
        assert "X-N7-Explicit" not in wire.requests[0].headers
        assert wire.requests[1].headers["X-N7-Explicit"] == "copy"
    finally:
        await copied.close()
        await client.close()


async def test_actual_fixture_refuses_ambient_sdk_settings(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", '{"X-N7-Poison":"invented"}')
    monkeypatch.setenv("ANTHROPIC_WEBHOOK_SIGNING_KEY", "n7-invented-signing-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://n7-poison.invalid")
    with pytest.raises(AmbientSDKConfigurationError):
        await setup(db_session, db_session_factory)


async def test_retained_anthropic_thread_replies_after_channel_switches_to_selfhosted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, dispatch, wire = await setup(db_session, db_session_factory)
    app.runtime = dataclasses.replace(
        app.runtime, turn_deps=dataclasses.replace(app.runtime.turn_deps, turn_path="mux")
    )
    try:
        first = await dispatch(THREAD, USER)
        async with db_session_factory.begin() as session:
            await set_channel_backend(
                session,
                channel_ref(tenant_id_from(first), "teams", CHANNEL),
                BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna"),
            )
        second = await dispatch(THREAD, USER)
        assert second.reused
        assert second.ma_session_id == first.ma_session_id
        assert revision_of(second) == revision_of(first)
        assert wire.stream_hits == [first.ma_session_id, first.ma_session_id]
        await persisted(db_session_factory, second, THREAD)
    finally:
        await app.runtime.anthropic.close()
