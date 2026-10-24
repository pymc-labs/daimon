"""Stopped-thread ownership: no alternate native ID reaches Anthropic."""

import json
import uuid
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.turn.prepare import PreparedTurn
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ProjectionSnapshot, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import UnsupportedCapability
from mux.profiles import get_profile
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage
from mux.testing.drivers.openai.conformance import OpenAIScriptedTransport
from mux.testing.isolated_sdk import build_async_openai

from .harness import make_bot


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["openai.persistent_workspace", "gemini.inline_reuse"])
@pytest.mark.parametrize(
    "case",
    [
        "cancel",
        "no_backend",
        "no_ref",
        "foreign_scope",
        "foreign_tenant",
        "foreign_kind",
        "foreign_grant",
        "foreign_provider",
        "foreign_profile",
        "recovered_id",
        "idle",
        "foreign_projection",
        "unsupported",
        "healthy",
        "native_admission_mismatch",
    ],
)
async def test_stopped_provider_turn_uses_only_its_authorized_root(
    profile: str, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = MagicMock()
    bot: DaimonBot = make_bot(runtime)
    thread_id = 70
    if case != "healthy":
        bot._thread_stops.signal(thread_id, reason="gone", code=10003, phase="running")  # pyright: ignore[reportPrivateUsage]
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    scope = Scope(
        tenant_id=str(tenant_id),
        account_id=str(account_id),
        principal_id="daimon",
        authorization_id="admitted-turn",
    )
    ref = ResourceRef(
        id="native-session",
        kind="session",
        provider=get_profile(profile).provider,
        account_scope_id="owned-provider-account",
        tenant_id=str(tenant_id),
        account_id=str(account_id),
    )
    backend = MagicMock()
    backend.capabilities.return_value = get_profile(profile)
    backend.events.reconcile = AsyncMock(
        return_value=ProjectionSnapshot(
            session=ref,
            cursor="1",
            state="running",
            active_root_turn="actual-native-root",
            taken_at=datetime.now(UTC),
        )
    )
    backend.events.cancel = AsyncMock()
    prepared = MagicMock(spec=PreparedTurn)
    prepared.admission = MagicMock()
    prepared.admission.account_id = account_id
    prepared.admission.grant = None
    prepared.admission.backend_revision.profile = profile
    prepared.session_account_id = account_id
    prepared.backend, prepared.session_ref = backend, ref
    session_id = ref.id
    if case == "no_backend":
        prepared.backend = None
    elif case == "no_ref":
        prepared.session_ref = None
    elif case == "foreign_scope":
        prepared.session_ref = ref.model_copy(update={"account_id": "other-account"})
    elif case == "foreign_tenant":
        prepared.session_ref = ref.model_copy(update={"tenant_id": "other-tenant"})
    elif case == "foreign_kind":
        prepared.session_ref = ref.model_copy(update={"kind": "agent"})
    elif case == "foreign_grant":
        prepared.admission.grant = MagicMock(tenant_id=uuid.uuid4())
    elif case == "foreign_provider":
        prepared.session_ref = ref.model_copy(update={"provider": "anthropic"})
    elif case == "foreign_profile":
        backend.capabilities.return_value = get_profile("anthropic.managed_agents")
    elif case == "recovered_id":
        session_id = "unbound-recovered-session"
    elif case == "idle":
        backend.events.reconcile.return_value = ProjectionSnapshot(
            session=ref, cursor="2", state="idle", taken_at=datetime.now(UTC)
        )
    elif case == "foreign_projection":
        backend.events.reconcile.return_value = ProjectionSnapshot(
            session=ref.model_copy(update={"id": "other-session"}),
            cursor="2",
            state="running",
            active_root_turn="foreign-root",
            taken_at=datetime.now(UTC),
        )
    elif case == "unsupported":
        backend.events.reconcile.side_effect = UnsupportedCapability(("cancel",), profile)
    elif case == "native_admission_mismatch":
        prepared.admission.backend_revision = None
    native = AsyncMock()
    monkeypatch.setattr("daimon.adapters.discord.bot.interrupt_orphaned_session", native)
    await bot._interrupt_stopped_turn(
        thread_id, tenant_id, cast(PreparedTurn, prepared), session_id, session_id, None
    )  # pyright: ignore[reportPrivateUsage]
    native.assert_not_awaited()
    if case == "cancel":
        backend.events.reconcile.assert_awaited_once_with(scope, ref)
        backend.events.cancel.assert_awaited_once()
        call = backend.events.cancel.await_args
        assert call.args == (scope, ref)
        assert call.kwargs["turn_id"] == "actual-native-root"
        assert uuid.UUID(call.kwargs["key"])
    else:
        backend.events.cancel.assert_not_awaited()
        if case not in {"idle", "foreign_projection", "unsupported"}:
            backend.events.reconcile.assert_not_awaited()


def prepared_for(backend: object, ref: ResourceRef, account_id: uuid.UUID) -> PreparedTurn:
    prepared = MagicMock(spec=PreparedTurn)
    prepared.admission = MagicMock()
    prepared.admission.account_id = account_id
    prepared.admission.grant = None
    prepared.admission.backend_revision.profile = (
        "openai.persistent_workspace" if ref.provider == "openai" else "gemini.inline_reuse"
    )
    prepared.session_account_id = account_id
    prepared.backend, prepared.session_ref = backend, ref
    return cast(PreparedTurn, prepared)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched", [False, True])
async def test_real_openai_stop_is_only_a_cancel_acknowledgement(
    mismatched: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    scope = Scope(
        tenant_id=str(tenant_id),
        account_id=str(account_id),
        principal_id="daimon",
        authorization_id="admitted-turn",
    )
    ref = ResourceRef(
        id="session",
        kind="session",
        provider="openai",
        account_scope_id="project",
        tenant_id=str(tenant_id),
        account_id=str(account_id),
    )
    script = OpenAIScriptedTransport()
    script.fixture, script.recovering = "C06", True
    cancels: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/v1/agents/sessions/session/events"
            assert json.loads(request.content) == {
                "events": [{"type": "agent.session.input.cancel"}]
            }
            assert uuid.UUID(request.headers["Idempotency-Key"])
            cancels.append(request)
            return httpx.Response(202)
        response = await script.handle(request)
        if request.url.path == "/v1/agents/sessions/session":
            # Seed the provider's ownership metadata with this test's admitted tenant.
            payload = response.json()
            payload["metadata"]["mux_tenant"] = str(tenant_id)
            return httpx.Response(200, json=payload)
        return response

    sdk = build_async_openai(
        transport=httpx.MockTransport(handle), base_url="https://openai.invalid/v1"
    )
    driver = OpenAIDriver(
        SDKTransport(sdk),
        account_scope_id="project",
        journal=MemoryRecoveryJournal(),
        usage_revisions=MemoryUsageRevisions(),
        authorization=lambda candidate, kind, id_: candidate == scope,
    )
    script.attach(driver, sdk)
    native = AsyncMock()
    monkeypatch.setattr("daimon.adapters.discord.bot.interrupt_orphaned_session", native)
    bot = make_bot(MagicMock())
    bot._thread_stops.signal(70, reason="gone", code=10003, phase="running")  # pyright: ignore[reportPrivateUsage]
    try:
        result = await bot._interrupt_stopped_turn(
            70,
            tenant_id,
            prepared_for(driver, ref, account_id),
            "unbound" if mismatched else ref.id,
        )  # pyright: ignore[reportPrivateUsage]
        assert result is None  # The adapter never claims an acknowledged cancel is a stop.
        native.assert_not_awaited()
        if mismatched:
            assert not cancels and not script.requests
        else:
            assert len(cancels) == 1
            current = await driver.sessions.retrieve(scope, ref)
            assert current.state == "running" and current.active_root_turn == "root"
    finally:
        await script.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched", [False, True])
async def test_real_gemini_stop_is_only_a_cancel_acknowledgement(
    mismatched: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, account_id = uuid.uuid4(), uuid.uuid4()
    scope = Scope(
        tenant_id=str(tenant_id),
        account_id=str(account_id),
        principal_id="daimon",
        authorization_id="admitted-turn",
    )
    transport = FakeTransport()
    driver = GeminiManagedAgents(
        transport,
        storage=MemoryStorage(),
        state_store=MemoryStateStore(),
        account_scope_id="project",
    )
    agent = await driver.agents.create(
        scope,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    environment = await driver.environments.create(scope, EnvironmentSpec(name="e"), key="env")
    thread = ThreadRef(
        channel=ChannelRef(tenant_id=str(tenant_id), platform="discord", channel_id="c"),
        thread_id="th",
    )
    session = await driver.sessions.create(
        scope,
        SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            environment=environment.ref,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={"thread": thread.model_dump(mode="json"), "binding_id": "b"},
                )
            },
        ),
        key="session",
    )
    stamp = datetime.now(UTC).isoformat()
    transport.responses.append(
        {
            "id": "actual-interaction",
            "status": "in_progress",
            "created": stamp,
            "updated": stamp,
            "environment_id": "e1",
            "steps": [],
            "usage": {
                "total_input_tokens": 10,
                "total_cached_tokens": 2,
                "total_output_tokens": 20,
                "total_thought_tokens": 3,
            },
        }
    )
    sent = await driver.events.send(
        scope, session.ref, (UserMessage(content=(TextPart(text="hello"),)),), key="first"
    )
    assert sent.turn_id is not None
    native = AsyncMock()
    monkeypatch.setattr("daimon.adapters.discord.bot.interrupt_orphaned_session", native)
    bot = make_bot(MagicMock())
    bot._thread_stops.signal(70, reason="gone", code=10003, phase="running")  # pyright: ignore[reportPrivateUsage]
    result = await bot._interrupt_stopped_turn(
        70,
        tenant_id,
        prepared_for(driver, session.ref, account_id),
        "unbound" if mismatched else session.ref.id,
    )  # pyright: ignore[reportPrivateUsage]
    assert result is None
    native.assert_not_awaited()
    assert transport.cancelled == ([] if mismatched else ["actual-interaction"])
    current = await driver.sessions.retrieve(scope, session.ref)
    assert current.state == "running" and current.active_root_turn == sent.turn_id
