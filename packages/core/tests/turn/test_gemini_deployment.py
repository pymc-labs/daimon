"""Offline app-runtime opt-in, explicit tools and durable Gemini proofs."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest
from daimon.core import pricing
from daimon.core.channel_backend import check_backend
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import PROFILE, GeminiJournal, GeminiUsageRuntime
from daimon.core.turn.gemini_deployment import GeminiPlan, build_gemini_runtime
from daimon.core.turn.gemini_state import SQLiteFlashAccounting, SQLiteGeminiStorage
from daimon.core.turn.prepare import ProviderPreparationRequest, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtimes import build_channel_runtime
from daimon.testing.factories import make_tenant
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.resources import Artifact
from mux.drivers.gemini.core import compile_agent
from mux.drivers.gemini.flash import FLASH_PRIMARY, FlashAttempt
from mux.drivers.gemini.storage import ArtifactRecord
from mux.drivers.gemini.transport import NO_TOOLS_TOOL
from mux.errors import ScopeViolation
from mux.profiles import selectable_models
from mux.profiles.gemini import ARGON_MODEL
from pydantic import JsonValue

from ..test_native_tariffs import fixed_source_date as fixed_source_date
from .test_agent_native_runtime import clone_source as clone_source
from .test_native_counterpart import clean_sdk_environment as clean_sdk_environment
from .test_native_counterpart import owned as owned
from .test_openai_deployment import application as application
from .test_openai_deployment import manifest


@pytest.fixture
async def selected(application: ProviderPreparationRequest) -> ProviderPreparationRequest:
    revision = application.admission.backend_revision
    assert revision is not None
    config = ConfigRevision.create(
        revision.channel,
        1,
        resolve_default(BackendConfig(backend="gemini", profile=PROFILE, model=FLASH_PRIMARY)),
    )
    return replace(
        application,
        admission=replace(
            application.admission, backend_revision=config, backend=check_backend(config)
        ),
        deps=replace(
            application.deps, state_store=PostgresStateStore(application.deps.sessionmaker)
        ),
    )


def entry(selected: ProviderPreparationRequest, tmp_path: Path) -> dict[str, JsonValue]:
    config = selected.admission.backend_revision
    assert config is not None
    return {
        "profile": PROFILE,
        "tenant_id": selected.scope.tenant_id,
        "platform": config.channel.platform,
        "channel_id": config.channel.channel_id,
        "account_id": selected.scope.account_id,
        "config_digest": config.digest,
        "project": "actual-operator-project",
        "api_key_env": "N4_OFFLINE_GEMINI_KEY",
        "records_path": str(tmp_path / "records.sqlite"),
        "holds_path": str(tmp_path / "holds.sqlite"),
        "hold_usd": "0.05",
        "cap_usd": "0.25",
        "agent_name": "explicit-gemini",
        "tools": [],
        "environment": {"name": "explicit-inline"},
    }


@pytest.fixture
def provider(request: pytest.FixtureRequest) -> Literal["openai", "gemini"]:
    return cast(Literal["openai", "gemini"], getattr(request, "param", "gemini"))


@pytest.mark.parametrize(
    "invalid",
    [
        "absent",
        "missing",
        "malformed",
        "oversize",
        "profile",
        "tools-missing",
        "tools-null",
        "tenant",
        "account",
        "platform",
        "channel",
        "digest",
        "duplicate",
        "inline-key",
        "relative",
        "same-db",
        "cap",
    ],
)
async def test_invalid_deployment_refuses_before_key_or_persistence(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid: str,
) -> None:
    row = entry(selected, tmp_path)
    if invalid == "profile":
        row["profile"] = "openai.persistent_workspace"
    elif invalid == "tools-missing":
        del row["tools"]
    elif invalid == "tools-null":
        row["tools"] = None
    elif invalid in ("tenant", "account", "platform", "channel", "digest"):
        row[
            {
                "tenant": "tenant_id",
                "account": "account_id",
                "platform": "platform",
                "channel": "channel_id",
                "digest": "config_digest",
            }[invalid]
        ] = "foreign"
    elif invalid == "inline-key":
        row["api_key"] = "never-show-secret"
    elif invalid == "relative":
        row["records_path"] = "relative.sqlite"
    elif invalid == "same-db":
        row["holds_path"] = row["records_path"]
    elif invalid == "cap":
        row["hold_usd"] = "1"
    manifest(
        monkeypatch, tmp_path / "manifest.json", [row, row] if invalid == "duplicate" else [row]
    )
    path = tmp_path / "manifest.json"
    if invalid == "absent":
        monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    elif invalid == "missing":
        path.unlink()
    elif invalid == "malformed":
        path.write_text("invalid-json")
    elif invalid == "oversize":
        path.write_bytes(b" " * 1_048_577)
    original = os.environ.get

    def get(name: str, default: str | None = None) -> str | None:
        if name == "N4_OFFLINE_GEMINI_KEY":
            pytest.fail("refused deployment accessed key")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", get)
    with pytest.raises(AdmissionDenied) as error:
        build_channel_runtime(selected)
    assert error.value.reason == "backend_unsupported" and "never-show-secret" not in str(
        error.value
    )
    assert not (tmp_path / "records.sqlite").exists() and not (tmp_path / "holds.sqlite").exists()


@pytest.mark.parametrize("tools", [[], [{"name": "google_search", "kind": "builtin"}]])
async def test_only_explicit_tools_compile_and_scope_is_pinned(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tools: list[dict[str, JsonValue]],
) -> None:
    row = entry(selected, tmp_path)
    row["tools"] = cast(JsonValue, tools)
    manifest(monkeypatch, tmp_path / "manifest.json", [row])
    runtime = build_channel_runtime(selected)
    assert isinstance(runtime.journal, GeminiJournal) and runtime.journal.deployment is not None
    native = compile_agent(runtime.journal.deployment.agent)
    assert native["tools"] == ([{"type": "google_search"}] if tools else [NO_TOOLS_TOOL])
    assert runtime.journal.state_store is selected.deps.state_store
    assert isinstance(runtime.usage_store, GeminiUsageRuntime)
    expected = {model: price for (_, model), price in pricing.provider_prices(PROFILE).items()}
    assert runtime.usage_store.prices == expected
    assert all(runtime.usage_store.prices[model] is price for model, price in expected.items())
    with pytest.raises(TypeError):
        runtime.usage_store.prices[FLASH_PRIMARY] = expected[FLASH_PRIMARY]
    # Inline reuse has no sandbox or container: its usage settles at $0 infrastructure.
    assert runtime.usage_store.infrastructure_usd == Decimal(0)
    assert not (tmp_path / "records.sqlite").exists()
    config = selected.admission.backend_revision
    assert config is not None
    with pytest.raises(ScopeViolation):
        runtime.transport_factory(
            config, selected.scope.model_copy(update={"account_id": "foreign"})
        )


async def test_storage_restart_rollback_full_bytes_and_tuple_keys(tmp_path: Path) -> None:
    path = tmp_path / "records.sqlite"

    def storage(namespace: str = "actual-account") -> SQLiteGeminiStorage:
        return SQLiteGeminiStorage(path, namespace=namespace)

    ref = ResourceRef(
        id="artifact",
        kind="artifact",
        provider="gemini",
        account_scope_id="project",
        tenant_id="tenant",
        account_id="account",
    )
    async with storage().transaction() as records:
        records.creations[("tenant", "account", "key")] = ("principal", "digest", "agent")
        records.artifacts["artifact"] = ArtifactRecord(
            Artifact(
                ref=ref,
                filename="binary.bin",
                media_type="application/octet-stream",
                size_bytes=3,
                created_at=datetime.now(UTC),
            ),
            "digest",
            b"\x00\xff\x80",
        )
    with pytest.raises(RuntimeError):
        async with storage().transaction() as records:
            records.creations.clear()
            raise RuntimeError("rollback")
    async with storage().transaction() as records:
        assert records.creations[("tenant", "account", "key")] == ("principal", "digest", "agent")
        assert records.artifacts["artifact"].body == b"\x00\xff\x80"
    async with storage("other-account").transaction() as records:
        assert not records.creations and not records.artifacts


async def test_storage_serializes_distinct_workers_and_recovers_after_cancellation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.sqlite"
    first, second = (SQLiteGeminiStorage(path, namespace="account") for _ in range(2))
    started = asyncio.Event()

    async def blocked() -> None:
        started.set()
        async with second.transaction() as records:
            records.creations[("t", "a", "second")] = ("p", "d", "two")

    async with first.transaction() as records:
        records.creations[("t", "a", "first")] = ("p", "d", "one")
        task = asyncio.create_task(blocked())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not task.done()
    await task
    async with first.transaction() as records:
        assert len(records.creations) == 2

    async def cancel() -> None:
        async with second.transaction() as records:
            records.creations.clear()
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancel()
    async with first.transaction() as records:
        assert len(records.creations) == 2


@pytest.mark.parametrize("outcome", ["accepted", "refused", "unknown"])
async def test_holds_survive_restart_and_refusal_never_releases_budget(
    tmp_path: Path, outcome: str
) -> None:
    path = tmp_path / "holds.sqlite"

    def accounting() -> SQLiteFlashAccounting:
        return SQLiteFlashAccounting(
            path, namespace="account", hold_usd=Decimal(".05"), cap_usd=Decimal(".05")
        )

    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    hold = accounting()
    await hold.before_attempt(model=model, attempt=1)
    await hold.after_attempt(
        receipt=FlashAttempt(model, 1, cast("Literal['accepted', 'refused', 'unknown']", outcome))
    )
    with pytest.raises(AdmissionDenied) as error:
        await accounting().before_attempt(model=model, attempt=1)
    assert error.value.reason == "provider_hold_cap_reached"


async def test_argon_attempt_holds_and_near_miss_ids_refuse(tmp_path: Path) -> None:
    hold = SQLiteFlashAccounting(
        tmp_path / "holds.sqlite",
        namespace="account",
        hold_usd=Decimal(".05"),
        cap_usd=Decimal(".10"),
    )
    for near in ("gemini-4-argon-preview", "gemini-4"):
        with pytest.raises(AdmissionDenied):
            await hold.before_attempt(model=ModelRef(provider="gemini", id=near), attempt=1)
    argon = ModelRef(provider="gemini", id=ARGON_MODEL)
    await hold.before_attempt(model=argon, attempt=1)
    await hold.after_attempt(receipt=FlashAttempt(argon, 1, "accepted", interaction_id="argon"))


async def test_unknown_hold_commits_before_native_dispatch_and_survives_crash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "holds.sqlite"
    hold = SQLiteFlashAccounting(
        path, namespace="account", hold_usd=Decimal(".05"), cap_usd=Decimal(".05")
    )
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    await hold.before_attempt(model=model, attempt=1)
    # Simulate process loss before any receipt. A new worker must preserve hold.
    restart = SQLiteFlashAccounting(
        path, namespace="account", hold_usd=Decimal(".05"), cap_usd=Decimal(".05")
    )
    with pytest.raises(AdmissionDenied):
        await restart.before_attempt(model=model, attempt=1)


@pytest.mark.parametrize("selection", ["unset", "anthropic", "legacy", "disabled"])
async def test_unconfigured_gemini_never_reads_manifest_or_any_key(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    original = os.environ.get

    def forbidden(name: str, default: str | None = None) -> str | None:
        if name in (
            "DAIMON_TURN__PROVIDER_RUNTIME_FILE",
            "N4_OFFLINE_GEMINI_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
        ):
            pytest.fail("default path discovered provider configuration")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", forbidden)
    request = selected
    if selection == "unset":
        request = replace(request, admission=replace(request.admission, backend_revision=None))
    elif selection == "anthropic":
        revision = selected.admission.backend_revision
        assert revision is not None
        request = replace(
            request,
            admission=replace(
                request.admission,
                backend_revision=ConfigRevision.create(
                    revision.channel, 1, resolve_default(BackendConfig())
                ),
            ),
        )
    else:
        request = replace(
            request,
            deps=replace(
                request.deps,
                **(
                    {"turn_path": "legacy"}
                    if selection == "legacy"
                    else {"channel_backends": False}
                ),
            ),
        )
    with pytest.raises(AdmissionDenied):
        build_channel_runtime(request)


async def test_policy_refusal_precedes_selected_key_and_transport(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest(monkeypatch, tmp_path / "manifest.json", [entry(selected, tmp_path)])
    original = os.environ.get

    def forbidden(name: str, default: str | None = None) -> str | None:
        if name == "N4_OFFLINE_GEMINI_KEY":
            pytest.fail("policy refusal inspected credential")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", forbidden)
    with pytest.raises(AdmissionDenied):
        await bind_session_impl(
            selected.deps,
            replace(selected.admission, asks_before_publishing=True),
            tenant_id=selected.tenant_id,
            platform=selected.platform,
            external_user_id=selected.external_user_id,
            thread_id=selected.thread_id,
            session_account_id=selected.session_account_id,
            reuse_existing=True,
        )
    assert not (tmp_path / "records.sqlite").exists()


def argon_clock(monkeypatch: pytest.MonkeyPatch, day: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # pyright: ignore[reportIncompatibleMethodOverride]
            assert tz is UTC
            return day

    monkeypatch.setattr(pricing, "datetime", Clock)


def argon_selected(
    selected: ProviderPreparationRequest, monkeypatch: pytest.MonkeyPatch, day: datetime
) -> ProviderPreparationRequest:
    """Select Argon on the channel and move the tariff clock to `day`."""
    argon_clock(monkeypatch, day)
    revision = selected.admission.backend_revision
    assert revision is not None
    config = ConfigRevision.create(
        revision.channel,
        1,
        resolve_default(BackendConfig(backend="gemini", profile=PROFILE, model=ARGON_MODEL)),
    )
    return replace(
        selected,
        admission=replace(
            selected.admission, backend_revision=config, backend=check_backend(config)
        ),
    )


@pytest.mark.parametrize(
    "output_cap,model", ((None, FLASH_PRIMARY), (4096, FLASH_PRIMARY), (None, ARGON_MODEL))
)
async def test_application_constructs_gemini_and_reuses_persisted_native_binding(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_clean: None,
    output_cap: int | None,
    model: str,
) -> None:
    import httpx

    from .test_gemini_codec import interaction

    if model == ARGON_MODEL:
        selected = argon_selected(selected, monkeypatch, datetime(2026, 11, 30, tzinfo=UTC))
    plan = entry(selected, tmp_path)
    if output_cap is not None:
        plan.update(
            max_output_tokens=output_cap, environment={"name": "no-execution", "execution": "none"}
        )
    manifest(monkeypatch, tmp_path / "manifest.json", [plan])
    monkeypatch.setenv("N4_OFFLINE_GEMINI_KEY", "offline-selected-key")
    calls: list[httpx.Request] = []
    posts: list[dict[str, JsonValue]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "offline-selected-key"
        if request.method == "POST":
            body = json.loads(request.content)
            # A capped model request has no built-ins; an agent allows the sentinel.
            assert body["tools"] == ([NO_TOOLS_TOOL] if output_cap is None else [])
            if output_cap is None:
                assert body["agent_config"]["model"] == model
                assert "generation_config" not in body
            else:
                assert body["model"] == model
                assert body["generation_config"] == {"max_output_tokens": 4096}
                assert not {"agent", "agent_config", "environment"} & body.keys()
            assert "spend_control" not in body
            posts.append(body)
            if len(posts) > 1:
                assert body["previous_interaction_id"] == "one"
                if output_cap is None:
                    assert body["environment"] == "workspace"
            response = interaction("one" if len(posts) == 1 else "two")
        else:
            response = interaction("two" if request.url.path.endswith("two") else "one")
        if output_cap is not None:
            response.pop("environment_id")
        return httpx.Response(200, json=response)

    # Replace owned HTTP transports only; exercise the real product SDK and host.
    from .test_gemini_runtime_transport import clients_for

    clients = clients_for(monkeypatch, handle)
    async with selected.deps.sessionmaker() as db, db.begin():
        await make_tenant(db, id=selected.tenant_id)
    deps = replace(selected.deps, state_store=None)

    async def forbidden_anthropic_backfill(*args: object, **kwargs: object) -> None:
        pytest.fail("foreign native sessions must not enter Anthropic billing catch-up")

    monkeypatch.setattr("daimon.core.turn.run.settle_session", forbidden_anthropic_backfill)

    session_id = None
    for index in range(2):
        prepared = await bind_session_impl(
            deps,
            selected.admission,
            tenant_id=selected.tenant_id,
            platform=selected.platform,
            external_user_id=selected.external_user_id,
            thread_id=selected.thread_id,
            session_account_id=selected.session_account_id,
            reuse_existing=True,
        )
        assert prepared.runtime is not None and isinstance(prepared.runtime.journal, GeminiJournal)
        deployment = prepared.runtime.journal.deployment
        assert deployment is not None and deployment.agent.model.id == model
        if session_id is not None:
            assert prepared.ma_session_id == session_id
        session_id = prepared.ma_session_id

        async def reseed() -> str:
            pytest.fail("Gemini turn used Anthropic recovery")

        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=selected.tenant_id,
            platform=selected.platform,
            thread_id=selected.thread_id,
            external_user_id=selected.external_user_id,
            user_message="question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            operation_key=f"offline-gemini:{index}",
        )
        assert outcome.state.error is None and not outcome.recovered
        from daimon.core.turn.state import extract_final_response

        assert extract_final_response(outcome.state.content) == "answer"
        assert outcome.state.usage_totals.output_tokens == 11
    assert len(posts) == 2 and calls and len(clients) == 2 * len(calls)
    assert all(client.is_closed for client in clients)
    assert (tmp_path / "records.sqlite").exists() and (tmp_path / "holds.sqlite").exists()
    # Inline reuse has no infrastructure charge, so its measured usage settles at once:
    # nothing pending, and the turn's cost is on the ledger (SMOKE fc0fa16a: every
    # Gemini usage row stayed pending forever, so no Gemini turn was ever debited).
    from sqlalchemy import text

    async with deps.sessionmaker() as db:
        assert (
            await db.scalar(text("SELECT count(*) FROM accounting_outbox WHERE applied_at IS NULL"))
            == 0
        )
        debited = await db.scalar(
            text("SELECT coalesce(sum(delta_usd), 0) FROM tenant_ledger WHERE delta_usd < 0")
        )
        assert debited < 0


async def test_argon_after_its_intro_tariff_refuses_before_runtime_discovery(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Admitted inside the window; the tariff expires before the runtime is built.
    selected = argon_selected(selected, monkeypatch, datetime(2026, 11, 30, tzinfo=UTC))
    argon_clock(monkeypatch, datetime(2026, 12, 1, tzinfo=UTC))
    getter = os.environ.get

    def no_manifest(name: str, default: str | None = None) -> str | None:
        if name == "DAIMON_TURN__PROVIDER_RUNTIME_FILE":
            pytest.fail("unpriced Argon discovered its runtime")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", no_manifest)
    with pytest.raises(AdmissionDenied) as refused:
        build_gemini_runtime(selected)
    assert refused.value.reason == "backend_unsupported"


async def test_cheap_model_does_not_enable_an_ordinary_channel_runtime(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    WORKER_MODEL = "gemini-3.5-flash-lite"

    revision = selected.admission.backend_revision
    assert revision is not None
    changed = revision.model_copy(update={"model": WORKER_MODEL})
    selected = replace(selected, admission=replace(selected.admission, backend_revision=changed))
    getter = os.environ.get

    def no_manifest(name: str, default: str | None = None) -> str | None:
        if name == "DAIMON_TURN__PROVIDER_RUNTIME_FILE":
            pytest.fail("unsupported ordinary model discovered its runtime")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", no_manifest)
    with pytest.raises(AdmissionDenied) as refused:
        build_gemini_runtime(selected)
    assert refused.value.reason == "backend_unsupported"


@pytest.mark.parametrize("model", selectable_models("gemini"))
def test_every_agent_selectable_model_composes_an_uncapped_host_turn(model: str) -> None:
    """What the card offers is what the host runs: an agent's turn is uncapped."""
    from daimon.core.mux_backend import TurnRuntime
    from daimon.core.turn.gemini import (  # pyright: ignore[reportPrivateUsage]
        GeminiDeployment,
        GeminiJournal,
        _runtime_model,
    )
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, EnvironmentSpec

    deployment = GeminiDeployment(
        "digest",
        1,
        "project",
        AgentSpec(name="agent", model=ModelRef(provider="gemini", id=model)),
        EnvironmentSpec(name="remote"),
    )
    runtime = TurnRuntime(
        transport_factory=lambda *_: None,
        journal=GeminiJournal(storage=None, state_store=None, deployment=deployment),  # pyright: ignore[reportArgumentType]
        usage_store=None,
    )
    assert _runtime_model(runtime) == model


@pytest.mark.parametrize("model", ["gemini-3.8-pro", "gemini-2.5-flash"])
async def test_an_unselectable_model_does_not_enable_an_ordinary_runtime(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    # Ordinary turns run the selectable models (Flash, Flash-Lite, Argon); others,
    # Pro included, refuse before any runtime discovery.
    revision = selected.admission.backend_revision
    assert revision is not None
    changed = revision.model_copy(update={"model": model})
    selected = replace(selected, admission=replace(selected.admission, backend_revision=changed))
    getter = os.environ.get

    def no_manifest(name: str, default: str | None = None) -> str | None:
        if name == "DAIMON_TURN__PROVIDER_RUNTIME_FILE":
            pytest.fail("unsupported ordinary model discovered its runtime")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", no_manifest)
    with pytest.raises(AdmissionDenied) as refused:
        build_gemini_runtime(selected)
    assert refused.value.reason == "backend_unsupported"


@pytest.mark.parametrize(
    "defect",
    ("bool", "zero", "above_max", "string", "sandbox", "sources", "network", "packages", "tools"),
)
async def test_capped_turn_refuses_invalid_controls_and_execution_before_key_access(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    defect: str,
) -> None:
    values = entry(selected, tmp_path)
    values.update(max_output_tokens=4096, environment={"name": "no-execution", "execution": "none"})
    if defect in ("bool", "zero", "above_max", "string"):
        values["max_output_tokens"] = {
            "bool": True,
            "zero": 0,
            "above_max": 65_537,
            "string": "4096",
        }[defect]
    elif defect == "tools":
        values["tools"] = [{"name": "code_execution", "kind": "builtin"}]
    else:
        env = dict(cast(dict[str, JsonValue], values["environment"]))
        if defect == "sandbox":
            env["execution"] = "hosted"
        elif defect == "sources":
            env["sources"] = [
                {
                    "kind": "repository",
                    "repository_url": "https://example.test/repo",
                    "target_path": "/workspace/repo",
                }
            ]
        elif defect == "network":
            env["network"] = {"mode": "disabled"}
        elif defect == "packages":
            env["packages"] = {"pip": ["package==1"]}
        values["environment"] = env
    manifest(monkeypatch, tmp_path / "manifest.json", [values])
    get = os.environ.get

    def key(name: str, default: str | None = None) -> str | None:
        if name == "N4_OFFLINE_GEMINI_KEY":
            pytest.fail("incompatible capped turn discovered its credential")
        return get(name, default)

    monkeypatch.setattr(os.environ, "get", key)
    with pytest.raises(AdmissionDenied) as refused:
        build_channel_runtime(selected)
    assert refused.value.reason == "backend_unsupported"
    assert not (tmp_path / "records.sqlite").exists()
    assert not (tmp_path / "holds.sqlite").exists()


def test_output_cap_omission_preserves_existing_plan_serialization(
    selected: ProviderPreparationRequest,
    tmp_path: Path,
) -> None:
    values = entry(selected, tmp_path)
    default = GeminiPlan.model_validate(values)
    omitted = GeminiPlan.model_validate({**values, "max_output_tokens": None})
    assert default.max_output_tokens is None
    assert default.model_dump_json() == omitted.model_dump_json()
    assert "max_output_tokens" not in json.loads(default.model_dump_json())


async def test_openai_selected_tariff_matches_real_default_direct_session_request(
    application, monkeypatch, tmp_path
):
    from daimon.core.turn.openai_host import driver

    from .test_openai_deployment import entry as openai_entry
    from .test_openai_host import Wire
    from .test_openai_runtime_transport import clients_for

    manifest(monkeypatch, tmp_path / "runtime.json", [openai_entry()])
    monkeypatch.setenv("N4_OFFLINE_OPENAI_KEY", "offline")
    wire = Wire()
    # The OpenAI profile has no native session cap: the row's limit isn't sent.
    wire.expected_spend_limit = None
    clients = clients_for(monkeypatch, wire.handle)
    runtime = build_channel_runtime(application)
    config = application.admission.backend_revision
    assert config is not None
    assert runtime.price is pricing.provider_prices(config.profile)[("openai", config.model)]
    backend = driver(runtime, config, application.scope)
    await backend.sessions.create(
        application.scope,
        await runtime.session_plan(application),
        key="openai:prepare:standard-session",
    )
    posts = [request for request in wire.requests if request.method == "POST"]
    assert len(posts) == 1
    request = posts[0]
    assert request.url.scheme == "https" and request.url.host == "api.openai.com"
    assert request.url.path == "/v1/agents/sessions" and not request.url.query
    body = json.loads(request.content)
    assert body["agent"]["model"] == config.model
    assert "service_tier" not in body and "service_tier" not in body["agent"]
    assert all(client.is_closed() for client in clients)


async def test_postgres_plan_is_explicit_and_refuses_ambiguous_storage(selected, tmp_path):
    from pydantic import ValidationError

    values = entry(selected, tmp_path)
    sqlite = GeminiPlan.model_validate(values)
    assert sqlite.storage_backend == "sqlite"
    assert "storage_backend" not in sqlite.model_dump(mode="json")
    postgres = dict(values, storage_backend="postgres")
    with pytest.raises(ValidationError, match="silently replace"):
        GeminiPlan.model_validate(postgres)
    postgres.pop("records_path")
    postgres.pop("holds_path")
    plan = GeminiPlan.model_validate(postgres)
    assert plan.records_path is None and plan.holds_path is None
    assert plan.model_dump(mode="json")["storage_backend"] == "postgres"
    for changes in ({"storage_backend": "unknown"}, {"storage_backend": "sqlite"}):
        with pytest.raises(ValidationError):
            GeminiPlan.model_validate(postgres | changes)


@pytest.mark.parametrize("marker", ["K_SERVICE", "CLOUD_RUN_JOB"])
async def test_cloud_run_sqlite_refuses_before_key_discovery(
    selected, tmp_path, monkeypatch, marker
):
    values = entry(selected, tmp_path)
    manifest(monkeypatch, tmp_path / "runtime.json", [values])
    monkeypatch.setenv(marker, "configured-host")
    original = os.environ.get

    def protected(name, default=None):
        if name == values["api_key_env"]:
            pytest.fail("ephemeral SQLite deployment discovered provider credential")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", protected)
    with pytest.raises(AdmissionDenied):
        build_gemini_runtime(selected)


@pytest.mark.parametrize("platform", ["teams", "discord", "slack"])
async def test_a_new_gemini_thread_sends_its_platforms_system_prompt_with_fragments_intact(
    platform: str,
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_clean: None,
) -> None:
    """The inline agent is created with this platform's variant of the plan's system, and
    the tenant's context-fragment block after it is sent byte for byte."""
    import re

    import yaml
    from daimon.core.agent_guidance import apply_credential_guidance
    from daimon.core.context_prompt import FRAGMENT_MARKER, ContextFragment, encode_fragments

    from .test_gemini_codec import interaction
    from .test_gemini_runtime_transport import clients_for

    config = selected.admission.backend_revision
    assert config is not None
    moved = ConfigRevision.create(
        config.channel.model_copy(update={"platform": platform}),
        1,
        resolve_default(BackendConfig(backend="gemini", profile=PROFILE, model=FLASH_PRIMARY)),
    )
    selected = replace(
        selected,
        platform=platform,
        admission=replace(selected.admission, backend_revision=moved, backend=check_backend(moved)),
    )
    defaults = Path(__file__).resolve().parents[4] / "defaults" / "agents" / "daimon.yaml"
    shipped = apply_credential_guidance(yaml.safe_load(defaults.read_text())["system"])
    other = "Slack" if platform != "slack" else "Discord"
    system = encode_fragments(
        shipped,
        {"routine": ContextFragment(text=f"Post the summary to {other} first.", mode="extend")},
    )
    plan = entry(selected, tmp_path)
    plan["system"] = system
    manifest(monkeypatch, tmp_path / "manifest.json", [plan])
    monkeypatch.setenv("N4_OFFLINE_GEMINI_KEY", "offline-selected-key")
    posts: list[dict[str, JsonValue]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
        return httpx.Response(200, json=interaction("one"))

    clients_for(monkeypatch, handle)
    async with selected.deps.sessionmaker() as db, db.begin():
        await make_tenant(db, id=selected.tenant_id)
    deps = replace(selected.deps, state_store=None)
    prepared = await bind_session_impl(
        deps,
        selected.admission,
        tenant_id=selected.tenant_id,
        platform=platform,
        external_user_id=selected.external_user_id,
        thread_id=selected.thread_id,
        session_account_id=selected.session_account_id,
        reuse_existing=True,
    )

    async def reseed() -> str:
        pytest.fail("Gemini turn used Anthropic recovery")

    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=selected.tenant_id,
        platform=platform,
        thread_id=selected.thread_id,
        external_user_id=selected.external_user_id,
        user_message="question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        operation_key="offline-gemini:platform-system",
    )

    sent = cast(str, posts[0]["system_instruction"])
    head, marker, fragments = sent.partition(FRAGMENT_MARKER)
    assert marker == FRAGMENT_MARKER
    assert fragments == system.partition(FRAGMENT_MARKER)[2], "the fragment block is intact"
    assert f"Post the summary to {other} first." in fragments
    foreign = "|".join(p for p in ("discord", "slack", "teams") if p != platform)
    assert re.search(foreign, head, re.IGNORECASE) is None
    assert head != shipped


async def test_a_used_up_daily_model_cap_refuses_before_google_is_called(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_clean: None,
) -> None:
    """Gemini usage reaches no ledger debit; the daily model cap stops the turn first."""
    import httpx

    from .test_gemini_runtime_transport import clients_for

    manifest(monkeypatch, tmp_path / "manifest.json", [entry(selected, tmp_path)])
    monkeypatch.setenv("N4_OFFLINE_GEMINI_KEY", "offline-selected-key")
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    clients_for(monkeypatch, handle)
    async with selected.deps.sessionmaker() as db, db.begin():
        await make_tenant(db, id=selected.tenant_id)
    deps = replace(selected.deps, state_store=None, gemini_daily_model_cap_usd=Decimal(0))

    with pytest.raises(AdmissionDenied) as refused:
        await bind_session_impl(
            deps,
            selected.admission,
            tenant_id=selected.tenant_id,
            platform=selected.platform,
            external_user_id=selected.external_user_id,
            thread_id=selected.thread_id,
            session_account_id=selected.session_account_id,
            reuse_existing=True,
        )
    model = selected.admission.backend_revision.model
    assert (refused.value.reason, refused.value.provider) == ("provider_daily_cap_reached", model)
    assert not [call for call in calls if call.method == "POST"], "nothing was created"
