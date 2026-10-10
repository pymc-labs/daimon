"""Offline app-runtime opt-in, explicit tools and durable Gemini proofs."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

import pytest
from daimon.core.channel_backend import check_backend
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import PROFILE, GeminiJournal, GeminiUsageRuntime
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
from mux.errors import ScopeViolation
from pydantic import JsonValue

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
    assert native["tools"] == ([{"type": "google_search"}] if tools else [])
    assert runtime.journal.state_store is selected.deps.state_store
    assert isinstance(runtime.usage_store, GeminiUsageRuntime) and not runtime.usage_store.prices
    assert runtime.usage_store.infrastructure_usd is None
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
    assert error.value.reason == "channel_budget_exceeded"


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


async def test_application_constructs_gemini_and_reuses_persisted_native_binding(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_clean: None,
) -> None:
    import httpx
    from mux.drivers.gemini import deployment

    from .test_gemini_codec import interaction

    manifest(monkeypatch, tmp_path / "manifest.json", [entry(selected, tmp_path)])
    monkeypatch.setenv("N4_OFFLINE_GEMINI_KEY", "offline-selected-key")
    for name, value in {
        "GOOGLE_API_KEY": "hostile-key",
        "GEMINI_API_KEY": "hostile-key",
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_GENAI_USE_ENTERPRISE": "true",
        "GOOGLE_GEMINI_BASE_URL": "https://hostile.invalid",
        "GEMINI_NEXT_GEN_API_BASE_URL": "https://hostile.invalid",
    }.items():
        monkeypatch.setenv(name, value)
    calls: list[httpx.Request] = []
    posts: list[dict[str, JsonValue]] = []
    clients: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "offline-selected-key"
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["tools"] == [] and body["agent_config"]["model"] == FLASH_PRIMARY
            assert "spend_control" not in body
            posts.append(body)
            if len(posts) > 1:
                assert (
                    body["previous_interaction_id"] == "one" and body["environment"] == "workspace"
                )
            return httpx.Response(200, json=interaction("one" if len(posts) == 1 else "two"))
        return httpx.Response(
            200, json=interaction("two" if request.url.path.endswith("two") else "one")
        )

    class OfflineClient(real_client):
        def __init__(self, *, trust_env: bool, timeout: float) -> None:
            assert trust_env is False
            super().__init__(
                transport=httpx.MockTransport(handle), trust_env=trust_env, timeout=timeout
            )
            clients.append(self)

    # Only HTTP network construction is substituted; SDK/loader/host are real.
    monkeypatch.setattr(deployment.httpx, "AsyncClient", OfflineClient)
    async with selected.deps.sessionmaker() as db, db.begin():
        await make_tenant(db, id=selected.tenant_id)
    deps = replace(selected.deps, state_store=None)
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
        assert outcome.state.usage_totals.output_tokens == 11
    assert len(posts) == 2 and calls and all(client.is_closed for client in clients)
    assert (tmp_path / "records.sqlite").exists() and (tmp_path / "holds.sqlite").exists()
    # Unknown tariffs and infrastructure are preserved as pending, not free.
    from sqlalchemy import text

    async with deps.sessionmaker() as db:
        assert (
            await db.scalar(text("SELECT count(*) FROM accounting_outbox WHERE applied_at IS NULL"))
            == 2
        )
