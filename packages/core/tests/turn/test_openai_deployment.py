"""Offline application runtime construction from explicit authorized deployment."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings, TurnSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn import openai_host
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_host import OpenAIHostRuntime
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.prepare import (
    DEFAULT_MA_CAPABILITIES,
    ProviderPreparationRequest,
    bind_session_impl,
)
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtimes import build_channel_runtime
from daimon.testing.factories import make_tenant
from daimon.testing.ma_transport import ScriptedTransport
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ThreadRef
from mux.drivers.openai import deployment
from mux.errors import ScopeViolation
from mux.state.lease import Slot
from openai import AsyncOpenAI
from pydantic import JsonValue, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_openai_host import Wire
from .test_provider_dispatch import ACCOUNT, PROFILE, REVISION, SCOPE, TENANT, admission


@pytest.fixture
async def application(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[ProviderPreparationRequest]:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setitem(Settings.model_config, "env_prefix", "N4_OFFLINE_UNUSED_")
    settings = Settings(
        anthropic=AnthropicSettings(api_key=SecretStr("offline")),
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://offline/unused")),
        turn=TurnSettings(path="mux", channel_backends=True),
    )
    native = ScriptedTransport()
    async with native.client() as client:
        deps = build_turn_deps(
            settings,
            client,
            db_session_factory,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            billing_config=None,
        )
        assert deps.turn_runtime_factory is build_channel_runtime
        assert not deps.turn_runtimes and deps.state_store is None
        yield ProviderPreparationRequest(
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
    assert native.requests == []


def entry() -> dict[str, JsonValue]:
    return {
        "profile": PROFILE,
        "tenant_id": SCOPE.tenant_id,
        "platform": "slack",
        "channel_id": "channel",
        "account_id": SCOPE.account_id,
        "config_digest": REVISION.digest,
        "project": "project",
        "agent_id": "native-agent",
        "environment_id": "native-template",
        "api_key_env": "N4_OFFLINE_OPENAI_KEY",
        "spend_limit_usd_cents": 4,
    }


def manifest(monkeypatch: pytest.MonkeyPatch, path: Path, rows: list[dict[str, JsonValue]]) -> None:
    path.write_text(json.dumps(rows))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))


@pytest.mark.parametrize("selection", ["unset", "anthropic", "legacy", "disabled", "gemini"])
async def test_unconfigured_application_never_discovers_provider_credentials(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    original = os.environ.get

    def forbidden(name: str, default: str | None = None) -> str | None:
        if name in ("DAIMON_TURN__PROVIDER_RUNTIME_FILE", "N4_OFFLINE_OPENAI_KEY"):
            raise AssertionError("unconfigured runtime inspected environment: " + name)
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", forbidden)
    selected = application.admission
    deps = application.deps
    if selection == "unset":
        selected = replace(selected, backend_revision=None)
    elif selection == "anthropic":
        selected = replace(
            selected,
            backend_revision=ConfigRevision.create(
                REVISION.channel, 1, resolve_default(BackendConfig())
            ),
        )
    elif selection == "legacy":
        deps = replace(deps, turn_path="legacy")
    elif selection == "disabled":
        deps = replace(deps, channel_backends=False)
    else:
        selected = replace(
            selected,
            backend_revision=ConfigRevision.create(
                REVISION.channel,
                1,
                resolve_default(
                    BackendConfig(
                        backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
                    )
                ),
            ),
        )
    with pytest.raises(AdmissionDenied):
        build_channel_runtime(replace(application, admission=selected, deps=deps))


@pytest.mark.parametrize(
    "invalid",
    [
        "absent",
        "missing",
        "malformed",
        "duplicate",
        "tenant",
        "platform",
        "account",
        "channel",
        "digest",
        "inline-secret",
        "controls",
        "large",
    ],
)
async def test_missing_or_foreign_deployment_refuses_without_credentials(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid: str,
) -> None:
    path = tmp_path / "runtime.json"
    row = entry()
    if invalid in ("tenant", "platform", "account", "channel", "digest"):
        row[
            {
                "tenant": "tenant_id",
                "platform": "platform",
                "account": "account_id",
                "channel": "channel_id",
                "digest": "config_digest",
            }[invalid]
        ] = "foreign"
    elif invalid == "inline-secret":
        row["api_key"] = "must-never-appear-in-errors"
    elif invalid == "controls":
        row["spend_limit_usd_cents"] = 0
    manifest(monkeypatch, path, [row, row] if invalid == "duplicate" else [row])
    if invalid == "absent":
        monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    elif invalid == "missing":
        path.unlink()
    elif invalid == "malformed":
        path.write_text("not json")
    elif invalid == "large":
        path.write_bytes(b" " * (1_048_576 + 1))
    original = os.environ.get

    def refuse_key(name: str, default: str | None = None) -> str | None:
        if name == "N4_OFFLINE_OPENAI_KEY":
            raise AssertionError("invalid deployment discovered a credential")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", refuse_key)
    with pytest.raises(AdmissionDenied) as error:
        build_channel_runtime(application)
    assert "must-never-appear-in-errors" not in str(error.value)
    assert "runtime.json" not in str(error.value)


async def test_plan_is_scoped_and_credential_access_is_deferred(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest(monkeypatch, tmp_path / "runtime.json", [entry()])
    monkeypatch.delenv("N4_OFFLINE_OPENAI_KEY", raising=False)
    runtime = build_channel_runtime(application)
    assert isinstance(runtime, OpenAIHostRuntime)
    assert isinstance(runtime.journal, OpenAIRecoveryJournal)
    assert isinstance(runtime.usage_store, OpenAIUsageRevisions)
    assert runtime.account_scope_id == "project" and runtime.price is None
    plan = await runtime.session_plan(application)
    assert plan.agent.id == "native-agent" and plan.agent.id != application.admission.agent.id
    assert plan.environment is not None and plan.environment.id == "native-template"
    assert plan.agent.tenant_id == SCOPE.tenant_id and plan.agent.account_id == SCOPE.account_id
    assert plan.config_revision == REVISION.local
    assert runtime.authorization(SCOPE, "agent", "native-agent")
    assert not runtime.authorization(SCOPE, "agent", "another-agent")
    foreign = SCOPE.model_copy(update={"account_id": "foreign"})
    assert not runtime.authorization(foreign, "session", "native-openai-session")
    with pytest.raises(ScopeViolation):
        runtime.transport_factory(REVISION, foreign)
    with pytest.raises(ScopeViolation):
        await runtime.session_plan(replace(application, scope=foreign))
    with pytest.raises(AdmissionDenied):
        runtime.transport_factory(REVISION, SCOPE)


async def test_omitted_native_spend_control_is_preserved(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    row = entry()
    del row["spend_limit_usd_cents"]
    manifest(monkeypatch, tmp_path / "runtime.json", [row])
    runtime = build_channel_runtime(application)
    assert isinstance(runtime, OpenAIHostRuntime)
    assert runtime.controls.spend_limit_usd_cents is None
    assert runtime.controls.multi_agent_enabled is False
    assert runtime.controls.container_size == "small"


async def test_other_channel_keys_and_plans_are_never_reused(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    other = entry()
    other["channel_id"] = "other-channel"
    other["agent_id"] = "other-native-agent"
    other["project"] = "other-project"
    other["api_key_env"] = "N4_OFFLINE_OTHER_KEY"
    manifest(monkeypatch, tmp_path / "runtime.json", [other, entry()])
    original = os.environ.get

    def refuse_other(name: str, default: str | None = None) -> str | None:
        if name == "N4_OFFLINE_OTHER_KEY":
            raise AssertionError("selected channel discovered another credential")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", refuse_other)
    runtime = build_channel_runtime(application)
    assert isinstance(runtime, OpenAIHostRuntime)
    plan = await runtime.session_plan(application)
    assert plan.agent.id == "native-agent" and runtime.account_scope_id == "project"
    next_revision = REVISION.model_copy(update={"local": 2, "digest": "changed-digest"})
    with pytest.raises(ScopeViolation):
        runtime.transport_factory(next_revision, SCOPE)
    with pytest.raises(AdmissionDenied):
        build_channel_runtime(
            replace(
                application,
                admission=replace(application.admission, backend_revision=next_revision),
            )
        )


async def test_denied_policy_never_reads_selected_key_or_constructs_transport(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest(monkeypatch, tmp_path / "runtime.json", [entry()])
    keys: list[str] = []
    original = os.environ.get

    def tracked(name: str, default: str | None = None) -> str | None:
        keys.append(name)
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", tracked)

    async def deny(deps: TurnDeps, selected: Admission) -> Admission:
        return replace(selected, memory_read_only=True)

    monkeypatch.setattr(openai_host, "reauthorize", deny)
    with pytest.raises(AdmissionDenied):
        await bind_session_impl(
            application.deps,
            application.admission,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )
    assert "N4_OFFLINE_OPENAI_KEY" not in keys


async def test_real_application_prepares_and_runs_using_builtin_runtime(
    application: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_clean: None,
) -> None:
    row = entry()
    del row["spend_limit_usd_cents"]
    manifest(monkeypatch, tmp_path / "runtime.json", [row])
    monkeypatch.setenv("N4_OFFLINE_OPENAI_KEY", "offline-only")
    wire = Wire()
    clients: list[AsyncOpenAI] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["OpenAI-Project"] == "project"
        if request.method == "POST" and request.url.path.endswith("/agents/sessions"):
            wire.requests.append(request)
            body = json.loads(request.content)
            assert "spend_control" not in body
            assert body["agent"] == {"model": "gpt-6-luna", "multi_agent": {"enabled": False}}
            assert body["environment"]["container_size"] == "small"
            assert request.headers["Idempotency-Key"].startswith("openai:prepare:")
            return httpx.Response(200, json=wire.session())
        return wire.handle(request)

    def sdk(
        *,
        api_key: str,
        project: str,
        organization: str,
        base_url: str,
        max_retries: int,
    ) -> AsyncOpenAI:
        assert api_key == "offline-only" and project == "project"
        assert organization == "" and base_url == "https://api.openai.com/v1" and max_retries == 0
        client = AsyncOpenAI(
            api_key=api_key,
            project=project,
            organization=organization,
            base_url=base_url,
            max_retries=max_retries,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )
        clients.append(client)
        return client

    # Only SDK network construction is replaced; the deployment loader, public
    # constructor, request-owned lifetimes, preparation and host turn are real.
    monkeypatch.setattr(deployment, "AsyncOpenAI", sdk)
    assert clients == []
    async with application.deps.sessionmaker() as db, db.begin():
        await make_tenant(db, id=TENANT)
    prepared = await bind_session_impl(
        application.deps,
        application.admission,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id="thread",
        session_account_id=ACCOUNT,
        reuse_existing=True,
    )
    assert isinstance(prepared.runtime, OpenAIHostRuntime)
    assert prepared.session_ref is not None and prepared.session_ref.provider == "openai"
    assert not application.deps.turn_runtimes and application.deps.state_store is None

    async def reseed() -> str:
        return "question"

    result = await run_prepared_turn(
        application.deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="caller",
        user_message="question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        render_interval_s=0.01,
        operation_key="deployment-host-turn",
    )
    await drain_outcomes()
    assert result.state.error is None and "answer turn-1" in str(result.state.content)
    store = PostgresStateStore(application.deps.sessionmaker)
    binding = await store.get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
            account_id=str(ACCOUNT),
        )
    )
    assert binding is not None and binding.native_refs["session"] == prepared.session_ref.id
    rows = await store.read_events(prepared.session_ref.id)
    assert any(event.type == "session.turn_ended" for event in rows)
    assert (
        sum(req.method == "POST" and req.url.path.endswith("/events") for req in wire.requests) == 1
    )
    assert all(source.closed for source in wire.sources)
    assert clients and all(client.is_closed() for client in clients)
    assert await store.pending_outbox()  # unpriced actual usage remains durable pending
