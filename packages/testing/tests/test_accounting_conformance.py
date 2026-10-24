"""The C12 hook runs actual neutral billing and rollback, without provider I/O."""

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, cast

import pytest
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.testing.accounting_conformance import HostAccountingHook, Snapshot
from daimon.testing.db import build_test_engine, isolated_test_schema
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.testing.conformance.fixtures import c12
from mux.testing.conformance.reference import create
from mux.testing.conformance.runner import Adapter, ConformanceFailure, PendingKind, run_fixture
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


@pytest.mark.parametrize(
    "database,host,query",
    [
        ("daimon_test", "localhost", ""),
        ("operator_contest", "localhost", ""),
        ("production", "localhost", ""),
        ("daimon_test_nc_", "localhost", ""),
        ("daimon_test_nc_n5-other", "localhost", ""),
        ("daimon_test_nc_" + "n" * 49, "localhost", ""),
        ("daimon_test_nc_N5", "remote.invalid", ""),
        ("daimon_test_nc_N5", "localhost", "?host=remote.invalid"),
    ],
)
async def test_c12_refuses_shared_non_lane_and_overridden_databases_before_connection(
    database: str, host: str, query: str
) -> None:
    engine = create_async_engine(f"postgresql+asyncpg://{host}/{database}{query}")
    attempts: list[object] = []

    def refuse_connection(*args: object) -> None:
        attempts.append(args)
        raise AssertionError("constructor must refuse before connecting")

    event.listen(engine.sync_engine, "do_connect", refuse_connection)
    try:
        with pytest.raises(ValueError, match="owned local daimon_test_nc_\\* lane database"):
            _guard_hook(engine)
        assert not attempts
    finally:
        await engine.dispose()


@pytest.mark.parametrize("lane", ["N5", "n9", "eval_n5", "review_n5_354b871"])
async def test_c12_accepts_owned_lane_database_names_without_connecting(lane: str) -> None:
    engine = create_async_engine(f"postgresql+asyncpg://localhost/daimon_test_nc_{lane}")
    attempts: list[object] = []

    def refuse_connection(*args: object) -> None:
        attempts.append(args)
        raise AssertionError("constructor must not connect")

    event.listen(engine.sync_engine, "do_connect", refuse_connection)
    try:
        hook = _guard_hook(engine)
        assert hook.engine is engine
        assert not attempts
    finally:
        await engine.dispose()


def _guard_hook(engine: AsyncEngine) -> HostAccountingHook:
    tenant = str(uuid.uuid4())
    scope = Scope(
        tenant_id=tenant,
        account_id="owned",
        principal_id="human",
        authorization_id="offline-db-guard",
    )
    session = ResourceRef(
        id="native-session",
        kind="session",
        provider="openai",
        account_scope_id="project",
        tenant_id=tenant,
        account_id="owned",
    )
    binding = ProviderBinding(
        id="owned-binding",
        provider="openai",
        profile="openai.fixture",
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=tenant, platform="slack", channel_id="channel"),
            thread_id="thread",
        ),
        native_refs={"session": session.id},
        generation=1,
        config_revision=1,
    )
    return HostAccountingHook(
        engine,
        scope,
        binding,
        session,
        ModelRef(provider="openai", id="offline-policy"),
        datetime(2026, 10, 10, tzinfo=UTC),
        "db-guard-only",
    )


@pytest.mark.parametrize(
    "github,ci,schema_kind,database,accepted",
    [
        ("true", "true", "worker", "daimon_test", True),
        ("true", "true", "fresh", "daimon_test", True),
        ("false", "true", "worker", "daimon_test", False),
        ("true", "false", "worker", "daimon_test", False),
        (None, "true", "worker", "daimon_test", False),
        ("true", None, "worker", "daimon_test", False),
        ("True", "true", "worker", "daimon_test", False),
        ("true", "true", "plain", "daimon_test", False),
        ("true", "true", "forged", "daimon_test", False),
        ("true", "true", "public", "daimon_test", False),
        ("true", "true", "custom", "daimon_test", False),
        ("true", "true", "foreign_pid", "daimon_test", False),
        ("true", "true", "worker", "operator_contest", False),
    ],
)
async def test_c12_ci_shared_name_requires_both_flags_and_owned_builder_schema(
    monkeypatch: pytest.MonkeyPatch,
    github: str | None,
    ci: str | None,
    schema_kind: str,
    database: str,
    accepted: bool,
) -> None:
    for name, value in (("GITHUB_ACTIONS", github), ("CI", ci)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    schema = {
        "worker": f"test_w{os.getpid()}_1234abcd",
        "fresh": f"test_f{os.getpid()}_1234abcd",
        "public": "public",
        "custom": "qa_custom_schema",
        "foreign_pid": f"test_w{os.getpid() + 1}_1234abcd",
    }.get(schema_kind)
    url = f"postgresql+asyncpg://localhost/{database}"
    engine = (
        build_test_engine(url, schema)
        if schema is not None
        else create_async_engine(
            url,
            execution_options={"isolated_test_schema": f"test_w{os.getpid()}_1234abcd"}
            if schema_kind == "forged"
            else {},
        )
    )
    attempts: list[object] = []

    def refuse_connection(*args: object) -> None:
        attempts.append(args)
        raise AssertionError("CI name policy must resolve before connecting")

    event.listen(engine.sync_engine, "do_connect", refuse_connection)
    try:
        if accepted:
            hook = _guard_hook(engine)
            assert hook.engine is engine
            assert isolated_test_schema(engine) == schema
        else:
            with pytest.raises(ValueError, match="owned local daimon_test_nc_\\* lane database"):
                _guard_hook(engine)
        assert not attempts
    finally:
        await engine.dispose()


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize("ci_name_policy", [False, True])
async def test_c12_hook_queries_production_accounting_and_connection_local_crash(
    db_preflight,
    db_engine: AsyncEngine,
    db_clean: None,
    provider: Literal["openai", "gemini"],
    monkeypatch: pytest.MonkeyPatch,
    ci_name_policy: bool,
) -> None:
    if db_engine.url.database == "daimon_test" and not (
        os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
    ):
        pytest.skip("guard admits only lane databases or isolated CI schemas")
    tenant = uuid.uuid4()
    scope = Scope(
        tenant_id=str(tenant), account_id="owned", principal_id="human", authorization_id="qa"
    )
    session_ref = ResourceRef(
        id="native-session",
        kind="session",
        provider=provider,
        account_scope_id="project",
        tenant_id=str(tenant),
        account_id="owned",
    )
    binding = ProviderBinding(
        id="owned-binding",
        provider=provider,
        profile=f"{provider}.fixture",
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=str(tenant), platform="slack", channel_id="channel"),
            thread_id="thread",
        ),
        native_refs={"session": session_ref.id},
        generation=1,
        config_revision=1,
    )
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await make_tenant(session, id=tenant)
    store = PostgresStateStore(sessions)
    await store.put_binding(binding, expected_generation=0)
    if ci_name_policy:
        # Simulate CI's URL metadata on the actual fixture-built engine. Its
        # already-created pool retains the original N5 DSN/connect arguments;
        # no local shared daimon_test connection is opened.
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("CI", "true")
        original_database = db_engine.url.database
        monkeypatch.setattr(db_engine.sync_engine, "url", db_engine.url.set(database="daimon_test"))
        async with sessions() as session:
            actual_database = await session.scalar(text("SELECT current_database()"))
            assert actual_database == original_database
            actual_schema = await session.scalar(text("SELECT current_schema()"))
            assert actual_schema == isolated_test_schema(db_engine)
    hook = HostAccountingHook(
        db_engine,
        scope,
        binding,
        session_ref,
        ModelRef(provider=provider, id="offline-policy"),
        datetime(2026, 10, 10, tzinfo=UTC),
        "host-c12",
    )
    # Driver is irrelevant to C12: this proves queried host effects, not native meters.
    adapter = create()
    captured = await hook()

    async def evidence():
        return captured

    monkeypatch.setattr(adapter.transport, "host_accounting_evidence", evidence, raising=False)
    deferred = Adapter(adapter.driver, store, adapter.transport, {"C12": hook.pending_reason})
    result = await run_fixture("C12", deferred)
    assert result.status == "pending"
    assert result.pending_reason == hook.pending_reason
    assert result.pending_reason is not None
    assert result.pending_reason.kind == PendingKind.ADAPTER_DEPENDENCY
    assert captured["pending_reason"] == hook.pending_reason
    # Existing C12 insists both sibling aggregates be persisted; neutral
    # ingestion rejects that arrangement. Never claim this fixture passed.
    with pytest.raises(ConformanceFailure, match="overlapping session/turn"):
        await c12(adapter.driver, store, adapter.transport)
    assert captured["aggregate_rejections"] == ("turn", "session")
    assert captured["aggregate_grains"] == ()
    assert captured["overlap_after"] == captured["overlap_before"]
    assert captured["source"] == "controlled_host_accounting"
    assert captured["projection_at_failure"] == 120
    assert captured["pending_after_failure"] == 1
    assert captured["pending_after_restart"] == 0
    assert captured["correction_channel_spend"] == Decimal("0.001100")
    assert not await store.pending_outbox()
    # Listener was connection-local and removed: a subsequent production write succeeds.
    assert await hook.record(hook.observation("post-crash-listener-check"))
    with pytest.raises(ConformanceFailure, match="fresh"):
        await hook()
    # No migration, trigger, default table policy or public-schema modification.
    async with sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'host_c12%'"
                )
            )
            == 0
        )
    assert captured["history_before"] == captured["history_after"]
    usage, ledger = cast(Snapshot, captured["corrections"])
    assert len(usage) == 1 and usage[0][4] == 110
    assert dict(ledger) == {
        "turn:native-session:host-c12:correction": Decimal("-0.001000"),
        "adjust:owned-binding:host-c12:correction:2": Decimal("-0.000200"),
        "adjust:owned-binding:host-c12:correction:3": Decimal("0.000100"),
    }
    assert captured["crash_before"] == captured["crash_after_failure"]
    recovered_usage, recovered_ledger = cast(Snapshot, captured["crash_after_restart"])
    assert len(recovered_usage) == 1 and recovered_usage[0][4] == 120
    assert dict(recovered_ledger) == {
        "turn:native-session:host-c12:crash": Decimal("-0.001000"),
        "adjust:owned-binding:host-c12:crash:2": Decimal("-0.000200"),
    }
    for changes in (
        {"session": session_ref.model_copy(update={"provider": "anthropic"})},
        {"session": session_ref.model_copy(update={"account_id": "foreign"})},
        {"model": ModelRef(provider="anthropic", id="offline-policy")},
        {"scope": scope.model_copy(update={"tenant_id": str(uuid.uuid4())})},
    ):
        with pytest.raises(ValueError):
            replace(hook, **changes)
