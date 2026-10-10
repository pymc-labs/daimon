"""Dispatch refuses foreign bindings before a provider can receive native IDs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest
from daimon.core import channel_backend, mux_backend
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.mux_backend import TurnBackend, TurnBackendRequest, TurnRuntime
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.turn import io as io_module
from daimon.core.turn import prepare as preparation
from daimon.core.turn import runtimes as runtime_module
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.io import LegacyTurnIO, TurnCodecRequest, TurnIO, turn_io
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.prepare import PreparedTurn, ProviderPreparationRequest, bind_session_impl
from daimon.core.turn.provider_actions import ProviderActionApproval, ProviderActionRequester
from daimon.core.turn.run import _turn_port_kwargs
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.state.memory import MemoryStateStore
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.UUID(int=4404)
ACCOUNT = uuid.UUID(int=4405)
SCOPE = Scope(
    tenant_id=str(TENANT),
    account_id=str(ACCOUNT),
    principal_id="daimon",
    authorization_id="admitted-turn",
)
PROFILE = "openai.persistent_workspace"
REVISION = ConfigRevision.create(
    ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="channel"),
    1,
    resolve_default(BackendConfig(backend="openai", profile=PROFILE, model="gpt-6-luna")),
)
SESSION = ResourceRef(
    id="native-openai-session",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id=SCOPE.tenant_id,
    account_id=SCOPE.account_id,
)
NOW = datetime.now(UTC)


@pytest.fixture
async def openai_backend() -> AsyncIterator[OpenAIDriver]:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected OpenAI I/O: {request.method} {request.url.path}")

    async with AsyncOpenAI(
        api_key="offline", http_client=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    ) as sdk:
        yield OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id=SESSION.account_scope_id,
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )


def deps_for(client: object, sm: async_sessionmaker[AsyncSession]) -> TurnDeps:
    from anthropic import AsyncAnthropic

    return TurnDeps(
        anthropic=cast(AsyncAnthropic, client),
        sessionmaker=sm,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=Path("/unused"),
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal(1),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        turn_path="mux",
    )


def admission() -> Admission:
    return Admission(
        account_id=ACCOUNT,
        agent=ma_agent(id="host-metadata-not-a-provider-agent"),
        environment=ma_environment(),
        config=ResolvedConfig(agent_name="test", environment_name="test"),
        backend_revision=REVISION,
    )


async def record_usage(event: object) -> None:
    raise AssertionError("dispatch must not bill")


def prepared(request: ProviderPreparationRequest, **changes: object) -> PreparedTurn:
    result = PreparedTurn(
        admission=request.admission,
        ma_session_id=SESSION.id,
        mapping_id=None,
        watermark=None,
        reused=False,
        session_account_id=request.session_account_id,
        _record=record_usage,
        session_ref=SESSION,
    )
    return replace(result, **changes)


@pytest.mark.parametrize("persistent", [False, True])
async def test_factory_and_codec_receive_model_native_identity_and_runtime(
    monkeypatch: pytest.MonkeyPatch, openai_backend: OpenAIDriver, persistent: bool
) -> None:
    transport = ScriptedTransport()
    runtime = TurnRuntime(
        lambda config, scope: (config, scope), MemoryRecoveryJournal(), MemoryUsageRevisions()
    )
    binding = ProviderBinding(
        id="provider-owner",
        thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
        provider="openai",
        profile=PROFILE,
        native_refs={"session": SESSION.id},
        generation=1,
        config_revision=REVISION.local,
        legacy_account_id=SCOPE.account_id,
    )
    persistence = (
        TurnPersistence(MemoryStateStore(), binding, SCOPE, operation_key="invocation")
        if persistent
        else None
    )
    factories: list[TurnBackendRequest] = []
    codecs: list[TurnCodecRequest] = []
    approval = ProviderActionApproval(
        ProviderActionRequester("slack", "thread", "requester"),
        NOW + timedelta(minutes=10),
        asyncio.Event(),
    )
    async with transport.client() as client:
        marker = LegacyTurnIO(client, "unused")

        def factory(request: TurnBackendRequest) -> TurnBackend:
            factories.append(request)
            return TurnBackend(openai_backend, SESSION)

        def codec(request: TurnCodecRequest) -> TurnIO:
            codecs.append(request)
            return marker

        monkeypatch.setitem(mux_backend._TURN_BACKENDS, PROFILE, factory)
        monkeypatch.setitem(io_module._TURN_CODECS, PROFILE, codec)
        request = TurnBackendRequest(
            PROFILE, client, SCOPE, SESSION.id, config=REVISION, session=SESSION, runtime=runtime
        )
        selected = turn_io(
            client,
            SESSION.id,
            path="mux",
            scope=SCOPE,
            profile=PROFILE,
            backend_request=request,
            persistence=persistence,
            provider_actions=approval,
        )
        expected = (
            replace(request, on_stop_event=persistence.record)
            if persistence is not None
            else request
        )
        assert selected is marker and factories == [expected]
        assert len(codecs) == 1
        assert codecs[0].provider_actions is not None
        assert codecs[0].provider_actions.approval is approval
        assert codecs[0].provider_actions.scope is SCOPE
        assert codecs[0].provider_actions.session is SESSION
        assert codecs[0].persistence is persistence
        assert codecs[0].model == "gpt-6-luna" and codecs[0].config == REVISION
        assert codecs[0].scope == SCOPE and codecs[0].session == SESSION
        assert codecs[0].runtime is not None
        assert codecs[0].runtime is runtime
        assert codecs[0].runtime.transport_factory(REVISION, SCOPE) == (REVISION, SCOPE)
        assert codecs[0].runtime.journal is runtime.journal
        assert codecs[0].runtime.usage_store is runtime.usage_store
    assert not transport.requests


@pytest.mark.parametrize("path", ["legacy", "mux"])
async def test_unregistered_selection_never_uses_anthropic(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(io_module._TURN_CODECS, PROFILE, raising=False)
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(UnsupportedCapability):
            turn_io(
                client,
                SESSION.id,
                path=cast(Literal["legacy", "mux"], path),
                scope=SCOPE,
                profile=PROFILE,
            )
    assert not transport.requests


@pytest.mark.parametrize("change", ["tenant", "account", "provider", "id", "kind", "profile"])
async def test_factory_cannot_replace_the_native_binding(
    change: str, monkeypatch: pytest.MonkeyPatch, openai_backend: OpenAIDriver
) -> None:
    transport = ScriptedTransport()
    altered = {
        "tenant": {"tenant_id": "foreign"},
        "account": {"account_id": "foreign"},
        "provider": {"provider": "anthropic"},
        "id": {"id": "foreign"},
        "kind": {"kind": "agent"},
        "profile": {},
    }[change]
    ref = SESSION.model_copy(update=altered)
    async with transport.client() as client:
        bound = TurnBackend(openai_backend, ref)
        monkeypatch.setitem(mux_backend._TURN_BACKENDS, PROFILE, lambda request: bound)
        selected_profile = "gemini.inline_reuse" if change == "profile" else PROFILE
        if change == "profile":
            monkeypatch.setitem(mux_backend._TURN_BACKENDS, selected_profile, lambda request: bound)
        request = TurnBackendRequest(selected_profile, client, SCOPE, SESSION.id, session=SESSION)
        with pytest.raises(ScopeViolation):
            mux_backend.turn_backend(request)
    assert not transport.requests


async def test_preparation_dispatches_before_any_anthropic_binding(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    transport = ScriptedTransport()
    calls: list[ProviderPreparationRequest] = []

    async def hook(request: ProviderPreparationRequest) -> PreparedTurn:
        calls.append(request)
        return prepared(request)

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, PROFILE, hook)
    async with transport.client() as client:
        deps = deps_for(client, db_session_factory)
        result = await bind_session_impl(
            deps,
            admission(),
            tenant_id=TENANT,
            platform="slack",
            external_user_id="user",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )
        assert result.session_ref == SESSION and result.ma_session_id == SESSION.id
        assert len(calls) == 1 and calls[0].scope == SCOPE
        assert calls[0].admission.backend_revision == REVISION
        kwargs = _turn_port_kwargs(
            deps,
            result.admission,
            result.ma_session_id,
            tenant_id=TENANT,
            provider_session=result.session_ref,
        )
        assert kwargs.get("profile") == PROFILE and kwargs.get("session_ref") == SESSION
        backend_request = kwargs.get("backend_request")
        assert backend_request is not None
        assert backend_request.session == SESSION
        assert backend_request.model == "gpt-6-luna"
    assert not transport.requests


@pytest.mark.parametrize("path", ["legacy", "mux"])
async def test_missing_preparation_fails_before_native_binding(
    path: str, db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(preparation._PROVIDER_PREPARATIONS, PROFILE, raising=False)
    transport = ScriptedTransport()
    async with transport.client() as client:
        deps = replace(
            deps_for(client, db_session_factory), turn_path=cast(Literal["legacy", "mux"], path)
        )
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            await bind_session_impl(
                deps,
                admission(),
                tenant_id=TENANT,
                platform="slack",
                external_user_id="user",
                thread_id="thread",
                session_account_id=ACCOUNT,
                reuse_existing=False,
            )
    assert not transport.requests


@pytest.mark.parametrize(
    "field", ["tenant", "account", "provider", "native-id", "no-ref", "config", "owner"]
)
async def test_foreign_provider_preparation_is_refused(
    field: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def hook(request: ProviderPreparationRequest) -> PreparedTurn:
        match field:
            case "tenant":
                return prepared(
                    request, session_ref=SESSION.model_copy(update={"tenant_id": "foreign"})
                )
            case "account":
                return prepared(
                    request, session_ref=SESSION.model_copy(update={"account_id": "foreign"})
                )
            case "provider":
                return prepared(
                    request, session_ref=SESSION.model_copy(update={"provider": "anthropic"})
                )
            case "native-id":
                return prepared(request, ma_session_id="ma-session")
            case "no-ref":
                return prepared(request, session_ref=None)
            case "config":
                return prepared(
                    request, admission=replace(request.admission, backend_revision=None)
                )
            case _:
                return prepared(request, session_account_id=uuid.UUID(int=4444))

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, PROFILE, hook)
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(ScopeViolation):
            await bind_session_impl(
                deps_for(client, db_session_factory),
                admission(),
                tenant_id=TENANT,
                platform="slack",
                external_user_id="user",
                thread_id="thread",
                session_account_id=ACCOUNT,
                reuse_existing=False,
            )
    assert not transport.requests


@pytest.mark.parametrize(
    ("backend", "profile", "model"),
    [("openai", PROFILE, "gpt-6-luna"), ("gemini", "gemini.inline_reuse", "gemini-3.8-flash")],
)
def test_explicit_provider_model_is_accepted_only_when_profile_enabled(
    backend: Literal["openai", "gemini"], profile: str, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(channel_backend, "RUNNABLE_PROFILES", frozenset())
    revision = ConfigRevision.create(
        REVISION.channel,
        1,
        resolve_default(BackendConfig(backend=backend, profile=profile, model=model)),
    )
    monkeypatch.setattr(
        channel_backend, "RUNNABLE_PROFILES", channel_backend.RUNNABLE_PROFILES - {profile}
    )
    with pytest.raises(channel_backend.BackendUnsupported):
        channel_backend.check_backend(revision)
    monkeypatch.setattr(channel_backend, "RUNNABLE_PROFILES", frozenset({profile}))
    assert channel_backend.check_backend(revision).profile_id == profile


async def test_missing_codec_does_not_construct_a_registered_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(io_module._TURN_CODECS, PROFILE, raising=False)

    def forbidden(request: TurnBackendRequest) -> TurnBackend:
        raise AssertionError("backend must not be constructed without a codec")

    monkeypatch.setitem(mux_backend._TURN_BACKENDS, PROFILE, forbidden)
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(UnsupportedCapability):
            turn_io(
                client, SESSION.id, path="mux", scope=SCOPE, profile=PROFILE, session_ref=SESSION
            )
    assert not transport.requests


async def test_anthropic_reference_is_rejected_before_an_openai_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(request: TurnBackendRequest) -> TurnBackend:
        raise AssertionError("a foreign native reference reached the provider factory")

    monkeypatch.setitem(mux_backend._TURN_BACKENDS, PROFILE, forbidden)
    transport = ScriptedTransport()
    async with transport.client() as client:
        request = TurnBackendRequest(
            PROFILE,
            client,
            SCOPE,
            SESSION.id,
            config=REVISION,
            session=SESSION.model_copy(update={"provider": "anthropic"}),
        )
        with pytest.raises(ScopeViolation):
            mux_backend.turn_backend(request)
    assert not transport.requests


async def test_preparation_ceiling_keeps_the_host_failure_contract(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from daimon.core.errors import TurnError

    async def timeout(request: ProviderPreparationRequest) -> PreparedTurn:
        raise TimeoutError

    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, PROFILE, timeout)
    transport = ScriptedTransport()
    async with transport.client() as client:
        with pytest.raises(TurnError) as error:
            await bind_session_impl(
                deps_for(client, db_session_factory),
                admission(),
                tenant_id=TENANT,
                platform="slack",
                external_user_id="user",
                thread_id="thread",
                session_account_id=ACCOUNT,
                reuse_existing=False,
            )
        assert error.value.kind == "ceiling"
    assert not transport.requests


def test_duplicate_registrations_refuse_to_replace_a_codec_or_preparer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def codec(request: TurnCodecRequest) -> TurnIO:
        raise AssertionError("not invoked during registration")

    async def prepare(request: ProviderPreparationRequest) -> PreparedTurn:
        raise AssertionError("not invoked during registration")

    monkeypatch.setitem(io_module._TURN_CODECS, PROFILE, codec)
    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, PROFILE, prepare)
    with pytest.raises(ValueError):
        io_module.register_turn_codec(PROFILE, codec)
    with pytest.raises(ValueError):
        preparation.register_turn_preparation(PROFILE, prepare)
    with pytest.raises(ValueError):
        preparation.register_turn_preparation("anthropic.managed_agents", prepare)


async def test_injected_backend_preserves_the_admitted_runtime(
    monkeypatch: pytest.MonkeyPatch, openai_backend: OpenAIDriver
) -> None:
    seen: list[TurnCodecRequest] = []
    transport = ScriptedTransport()
    async with transport.client() as client:
        marker = LegacyTurnIO(client, "unused")

        def codec(request: TurnCodecRequest) -> TurnIO:
            seen.append(request)
            return marker

        monkeypatch.setitem(io_module._TURN_CODECS, PROFILE, codec)
        runtime = TurnRuntime(
            lambda config, scope: (config, scope), MemoryRecoveryJournal(), MemoryUsageRevisions()
        )
        request = TurnBackendRequest(
            PROFILE, client, SCOPE, SESSION.id, config=REVISION, session=SESSION, runtime=runtime
        )
        assert (
            turn_io(
                client,
                SESSION.id,
                path="mux",
                scope=SCOPE,
                profile=PROFILE,
                backend=openai_backend,
                session_ref=SESSION,
                backend_request=request,
            )
            is marker
        )
        assert seen[0].runtime is runtime and seen[0].config == REVISION
        with pytest.raises(ScopeViolation):
            turn_io(
                client,
                SESSION.id,
                path="mux",
                scope=SCOPE,
                profile=PROFILE,
                backend=openai_backend,
                session_ref=SESSION,
                backend_request=replace(
                    request, session=SESSION.model_copy(update={"account_scope_id": "foreign"})
                ),
            )
    assert not transport.requests


async def test_injected_backend_cannot_bypass_default_channel_admission(
    openai_backend: OpenAIDriver, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    transport = ScriptedTransport()

    def native_session(id_: str, scope: Scope) -> ResourceRef:
        return SESSION

    async with transport.client() as client:
        deps = replace(
            deps_for(client, db_session_factory),
            backend=openai_backend,
            backend_session_ref=native_session,
        )
        default = replace(admission(), backend_revision=None)
        with pytest.raises(ScopeViolation, match="admitted profile"):
            _turn_port_kwargs(deps, default, SESSION.id, tenant_id=TENANT)
    assert not transport.requests


@pytest.mark.parametrize("lookup", [True, False])
@pytest.mark.parametrize("injected", [True, False])
async def test_application_runtime_is_lazy_scoped_and_carried_into_execution(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    lookup: bool,
    injected: bool,
) -> None:
    from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings, TurnSettings
    from daimon.core.turn.deps import build_turn_deps
    from pydantic import PostgresDsn, SecretStr

    runtime = TurnRuntime(lambda config, scope: (config, scope), object(), object())
    requests: list[ProviderPreparationRequest] = []

    def factory(request: ProviderPreparationRequest) -> TurnRuntime:
        requests.append(request)
        assert request.admission.backend_revision == REVISION
        assert request.scope == SCOPE
        assert request.deps.state_store is not None
        return runtime

    async def hook(request: ProviderPreparationRequest) -> PreparedTurn:
        if lookup:
            assert request.deps.turn_runtimes.get(PROFILE) is runtime
            assert request.deps.turn_runtimes.get(PROFILE) is runtime
            assert request.deps.turn_runtimes.get("gemini.inline_reuse") is None
        return prepared(request)

    monkeypatch.setitem(runtime_module._RUNTIME_FACTORIES, PROFILE, factory)
    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, PROFILE, hook)
    # An isolated prefix and no .env file keep this validated offline config
    # separate from deployment credentials.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setitem(Settings.model_config, "env_prefix", "N4_OFFLINE_UNUSED_")
    settings = Settings(
        anthropic=AnthropicSettings(api_key=SecretStr("offline")),
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://offline/unused")),
        turn=TurnSettings(path="mux", channel_backends=True),
    )
    transport = ScriptedTransport()
    async with transport.client() as client:
        deps = build_turn_deps(
            settings,
            client,
            db_session_factory,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            billing_config=None,
        )
        assert not requests and not deps.turn_runtimes
        if injected:
            deps = replace(deps, turn_runtimes={PROFILE: runtime})
        result = await bind_session_impl(
            deps,
            admission(),
            tenant_id=TENANT,
            platform="slack",
            external_user_id="user",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )
        assert len(requests) == int(lookup and not injected)
        assert result.runtime is (runtime if lookup or injected else None)
        assert dict(deps.turn_runtimes) == ({PROFILE: runtime} if injected else {})
        kwargs = _turn_port_kwargs(
            deps,
            result.admission,
            result.ma_session_id,
            tenant_id=TENANT,
            provider_session=result.session_ref,
            provider_runtime=result.runtime,
        )
        backend_request = kwargs.get("backend_request")
        assert backend_request is not None and backend_request.runtime is result.runtime
    assert not transport.requests


async def test_missing_runtime_constructor_refuses_without_provider_or_key_access(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def hook(request: ProviderPreparationRequest) -> PreparedTurn:
        request.deps.turn_runtimes.get(PROFILE)
        raise AssertionError("an absent deployment must refuse")

    monkeypatch.setattr(runtime_module, "_RUNTIME_FACTORIES", {})
    monkeypatch.setitem(preparation._PROVIDER_PREPARATIONS, PROFILE, hook)
    transport = ScriptedTransport()
    async with transport.client() as client:
        deps = replace(
            deps_for(client, db_session_factory),
            channel_backends=True,
            turn_runtime_factory=runtime_module.build_channel_runtime,
        )
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            await bind_session_impl(
                deps,
                admission(),
                tenant_id=TENANT,
                platform="slack",
                external_user_id="user",
                thread_id="thread",
                session_account_id=ACCOUNT,
                reuse_existing=True,
            )
    assert not transport.requests


@pytest.mark.parametrize(
    "invalid", ["no-config", "anthropic", "disabled", "legacy", "tenant", "model"]
)
async def test_channel_runtime_refuses_invalid_admission_before_constructor(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    invalid: str,
) -> None:
    def forbidden(request: ProviderPreparationRequest) -> TurnRuntime:
        raise AssertionError("invalid admission reached runtime/credential construction")

    monkeypatch.setitem(runtime_module._RUNTIME_FACTORIES, PROFILE, forbidden)
    transport = ScriptedTransport()
    async with transport.client() as client:
        deps = replace(deps_for(client, db_session_factory), channel_backends=True)
        selected = admission()
        scope = SCOPE
        if invalid == "no-config":
            selected = replace(selected, backend_revision=None)
        elif invalid == "anthropic":
            selected = replace(
                selected,
                backend_revision=ConfigRevision.create(
                    REVISION.channel, 1, resolve_default(BackendConfig())
                ),
            )
        elif invalid == "disabled":
            deps = replace(deps, channel_backends=False)
        elif invalid == "legacy":
            deps = replace(deps, turn_path="legacy")
        elif invalid == "tenant":
            scope = SCOPE.model_copy(update={"tenant_id": "foreign"})
        else:
            selected = replace(
                selected, backend_revision=REVISION.model_copy(update={"model": None})
            )
        request = ProviderPreparationRequest(
            deps,
            selected,
            scope,
            TENANT,
            "slack",
            "user",
            "thread",
            ACCOUNT,
            True,
            preparation.DEFAULT_MA_CAPABILITIES,
            None,
            NOW,
            lambda: NOW,
        )
        with pytest.raises((AdmissionDenied, ScopeViolation)):
            runtime_module.build_channel_runtime(request)
    assert not transport.requests


async def test_scoped_runtime_lookup_never_shares_a_profile_between_channel_revisions(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seen: list[ProviderPreparationRequest] = []
    constructed: list[TurnRuntime] = []

    def factory(request: ProviderPreparationRequest) -> TurnRuntime:
        seen.append(request)
        runtime = TurnRuntime(lambda config, scope: (config, scope), object(), object())
        constructed.append(runtime)
        return runtime

    transport = ScriptedTransport()
    async with transport.client() as client:
        deps = deps_for(client, db_session_factory)
        lookups: list[runtime_module.ScopedTurnRuntimes] = []
        for revision in (REVISION, REVISION.model_copy(update={"local": 2, "digest": "second"})):
            selected = replace(admission(), backend_revision=revision)
            request = ProviderPreparationRequest(
                deps,
                selected,
                SCOPE,
                TENANT,
                "slack",
                "user",
                "thread",
                ACCOUNT,
                True,
                preparation.DEFAULT_MA_CAPABILITIES,
                None,
                NOW,
                lambda: NOW,
            )
            lookup = runtime_module.ScopedTurnRuntimes(request, factory)
            assert lookup.resolved is None and list(lookup) == [PROFILE]
            assert len(lookup) == 1 and lookup.get("gemini.inline_reuse") is None
            lookups.append(lookup)
        assert not seen and not constructed
        first, second = (lookup.get(PROFILE) for lookup in lookups)
        assert first is constructed[0] and second is constructed[1] and first is not second
        assert [
            request.admission.backend_revision.local
            for request in seen
            if request.admission.backend_revision
        ] == [1, 2]
        assert lookups[0].get(PROFILE) is first and lookups[1].get(PROFILE) is second
        assert len(seen) == 2 and not deps.turn_runtimes
    assert not transport.requests
