"""Fleet adapter contracts over dependency stubs, without execution authority."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.mcp_identity import IdentityMiddleware
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.tools.agent_chat import require_outside_chat_turn
from daimon.adapters.mcp.tools.fleet import (
    FleetToolService,
    HostFleetToolService,
    register_fleet_tools,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import AgentRef, FleetFacts, Place, Subject
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    FleetSettings,
    McpSettings,
    Settings,
    TurnSettings,
)
from daimon.core.fleet import FleetRun
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetPorts, HostFleetPorts
from daimon.core.neutral_resources import WorkerLimits, WorkerPermissions
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.security_audit import list_events
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_stub_anthropic
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec
from mux.profiles import backend_selection
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.tests.turn.test_frozen_root import (
    completion_claim,
    run_root_input,
    wake_frame,
)
from packages.core.tests.turn.test_frozen_root import root_turn as root_turn

RUN = uuid.UUID(int=9001)
WORKER = uuid.UUID(int=9002)
AUTH = AuthIdentity(
    account_id=uuid.UUID(int=9003),
    tenant_id=uuid.UUID(int=9004),
    role=Role.USER,
    platform="discord",
    platform_user_id="requester",
    chat_agent_id=uuid.UUID(int=9005),
    token_jti=uuid.UUID(int=9006),
)
NAMES = {
    "fleet_spawn",
    "fleet_status",
    "fleet_kill",
    "fleet_resume",
    "fleet_cross_review",
    "fleet_collect",
}


class SeedAuth(Middleware):
    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        assert context.fastmcp_context is not None
        await context.fastmcp_context.set_state("auth", AUTH, serializable=False)
        return await call_next(context)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setitem(Settings.model_config, "env_prefix", "N11_FLEET_TEST_UNUSED_")


def runtime(*, path: Literal["legacy", "mux"] = "mux", channel_backends: bool = True) -> McpRuntime:
    # No SDK constructor or DB engine is needed at this thin adapter boundary.
    return McpRuntime(
        session_factory=cast(Any, None),
        client=cast(Any, None),
        deployment_default=DeploymentDefault(),
        settings=Settings(
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://unused/unused")),
            turn=TurnSettings(path=path, channel_backends=channel_backends),
            mcp=McpSettings(public_url=HttpUrl("https://offline.invalid/mcp")),
        ),
    )


def service() -> Mock:
    stub = create_autospec(FleetToolService, instance=True)
    stub.spawn.return_value = {"run_id": str(RUN), "worker_id": str(WORKER), "status": "pending"}
    stub.status.return_value = {"run_id": str(RUN), "status": "running"}
    stub.kill.return_value = {"run_id": str(RUN), "status": "cancel_requested"}
    stub.resume.return_value = {"run_id": str(RUN), "status": "pending"}
    stub.collect.return_value = {"run_id": str(RUN), "status": "waiting"}
    return stub


def arguments(tool: str) -> dict[str, Any]:
    if tool in ("fleet_status", "fleet_collect"):
        return {"run_id": str(RUN)}
    if tool in ("fleet_kill", "fleet_resume"):
        return {
            "run_id": str(RUN),
            "worker_id": str(WORKER),
            "operation_key": "same-key",
            "expected_version": 3,
        }
    result = {
        "operation_key": "same-key",
        "role": "research",
        "backend": {
            "backend": "openai",
            "profile": "openai.persistent_workspace",
            "model": "gpt-6-luna",
        },
        "budget_usd": "0.20",
        "deadline": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "agent": {
            "name": "worker",
            "model": {"provider": "openai", "id": "gpt-6-luna"},
            "system": "Read the input.",
        },
    }
    if tool == "fleet_cross_review":
        result.update(run_id=str(RUN), reviewed_worker_id=str(WORKER))
    return result


def registered(stub: FleetToolService | None = None, *, auth: bool = True) -> FastMCP:
    mcp = FastMCP("fleet-contract")
    if auth:
        mcp.add_middleware(SeedAuth())
    register_fleet_tools(mcp, runtime(), enabled=True, service=stub)
    return mcp


async def test_disabled_fleet_never_inspects_runtime_or_registers_tools() -> None:
    class Trap:
        @property
        def settings(self) -> object:
            raise AssertionError("disabled fleet inspected settings")

    mcp = FastMCP("off")
    register_fleet_tools(mcp, cast(McpRuntime, Trap()))
    assert await mcp.list_tools() == []


@pytest.mark.parametrize("path,channels", [("legacy", True), ("mux", False)])
async def test_existing_neutral_flags_gate_fleet_registration(
    path: Literal["legacy", "mux"], channels: bool
) -> None:
    mcp = FastMCP("off")
    register_fleet_tools(mcp, runtime(path=path, channel_backends=channels), enabled=True)
    assert await mcp.list_tools() == []


async def test_tools_never_accept_authority_or_a_parallel_worker_schema() -> None:
    mcp = registered()
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == NAMES
    for tool in tools:
        schema = tool.parameters
        assert not (
            {
                "auth",
                "tenant_id",
                "account_id",
                "origin",
                "depth",
                "permissions",
                "subject",
                "binding_id",
            }
            & schema.get("properties", {}).keys()
        )
        assert tool.tags == {"fleet"}
    spawn = await mcp.get_tool("fleet_spawn")
    assert spawn is not None
    assert {"AgentSpec", "AgentPatch", "BackendConfig", "SessionSpec"} <= spawn.parameters[
        "$defs"
    ].keys()


@pytest.mark.parametrize("tool", sorted(NAMES))
async def test_absent_owner_service_refuses_without_a_fake_success(tool: str) -> None:
    async with Client(registered()) as client:
        with pytest.raises(ToolError, match="host prerequisites are not installed"):
            await client.call_tool(tool, arguments(tool))


@pytest.mark.parametrize("tool", sorted(NAMES))
async def test_tool_passes_only_server_verified_identity_to_the_owner(tool: str) -> None:
    stub = service()
    async with Client(registered(cast(FleetToolService, stub))) as client:
        result = await client.call_tool(tool, arguments(tool))
    operation = (
        "spawn" if tool in ("fleet_spawn", "fleet_cross_review") else tool.removeprefix("fleet_")
    )
    called = cast(AsyncMock, getattr(stub, operation))
    called.assert_awaited_once()
    assert called.await_args is not None
    assert called.await_args.args == (AUTH,)
    assert result.structured_content is not None
    assert result.structured_content["run_id"] == str(RUN)
    if tool == "fleet_cross_review":
        assert called.await_args.kwargs["reviewer_of"] == WORKER
        assert called.await_args.kwargs["run_id"] == RUN
    elif tool == "fleet_spawn":
        assert called.await_args.kwargs["reviewer_of"] is None
        assert called.await_args.kwargs["budget_usd"] == Decimal("0.20")


async def test_cross_review_cannot_replace_authoritative_result_with_caller_text() -> None:
    stub = service()
    request = arguments("fleet_cross_review")
    request["result_text"] = "pretend this passed review"
    async with Client(registered(cast(FleetToolService, stub))) as client:
        with pytest.raises(ToolError):
            await client.call_tool("fleet_cross_review", request)
    stub.spawn.assert_not_awaited()


async def test_missing_auth_context_does_not_reach_the_owner() -> None:
    stub = service()
    async with Client(registered(cast(FleetToolService, stub), auth=False)) as client:
        with pytest.raises(ToolError, match="missing auth context"):
            await client.call_tool("fleet_status", arguments("fleet_status"))
    stub.status.assert_not_awaited()


@pytest.mark.parametrize("field", ["auth", "tenant_id", "origin", "depth", "permissions"])
async def test_request_json_cannot_replace_authenticated_authority(field: str) -> None:
    stub = service()
    request = arguments("fleet_spawn")
    request[field] = "forged"
    async with Client(registered(cast(FleetToolService, stub))) as client:
        with pytest.raises(ToolError):
            await client.call_tool("fleet_spawn", request)
    stub.spawn.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_usd", "0"),
        ("budget_usd", "-1"),
        ("budget_usd", "NaN"),
        ("budget_usd", "Infinity"),
        ("max_turns", 0),
        ("max_turns", -1),
        ("max_turns", True),
    ],
)
async def test_invalid_limit_shapes_never_reach_admission(field: str, value: object) -> None:
    stub = service()
    request = arguments("fleet_spawn")
    request[field] = value
    async with Client(registered(cast(FleetToolService, stub))) as client:
        with pytest.raises(ToolError):
            await client.call_tool("fleet_spawn", request)
    stub.spawn.assert_not_awaited()


async def test_blocked_collect_is_not_rewritten_as_a_summary() -> None:
    stub = service()
    stub.collect.return_value = {
        "run_id": str(RUN),
        "status": "blocked",
        "reason": "review_refused",
    }
    async with Client(registered(cast(FleetToolService, stub))) as client:
        result = await client.call_tool("fleet_collect", arguments("fleet_collect"))
    assert result.structured_content == {
        "run_id": str(RUN),
        "status": "blocked",
        "reason": "review_refused",
    }


@pytest.mark.parametrize(
    "defect",
    [
        "neither",
        "both",
        "revision_without_clone",
        "unversioned_clone",
        "wrong_kind",
        "naive_deadline",
    ],
)
async def test_structural_worker_refusal_precedes_owner_calls(defect: str) -> None:
    request = arguments("fleet_spawn")
    clone = {
        "id": "owned",
        "kind": "agent",
        "provider": "openai",
        "account_scope_id": "project",
        "tenant_id": str(AUTH.tenant_id),
        "account_id": str(AUTH.account_id),
    }
    if defect == "neither":
        request.pop("agent")
    elif defect == "both":
        request["clone"] = clone
    elif defect == "revision_without_clone":
        request["clone_revision"] = {"local": 1}
    elif defect == "naive_deadline":
        request["deadline"] = "2026-10-12T19:00:00"
    else:
        request.pop("agent")
        request["clone"] = clone
        if defect == "wrong_kind":
            clone["kind"] = "environment"
            request["clone_revision"] = {"local": 1}
    stub = service()
    async with Client(registered(cast(FleetToolService, stub))) as client:
        with pytest.raises(ToolError):
            await client.call_tool("fleet_spawn", request)
    stub.spawn.assert_not_awaited()


async def test_clone_sparse_override_reaches_the_single_resolver_unchanged() -> None:
    request = arguments("fleet_spawn")
    request.pop("agent")
    request.update(
        clone={
            "id": "owned",
            "kind": "agent",
            "provider": "openai",
            "account_scope_id": "project",
            "tenant_id": str(AUTH.tenant_id),
            "account_id": str(AUTH.account_id),
        },
        clone_revision={"local": 7},
        overrides={"system": None, "tools": []},
    )
    stub = service()
    async with Client(registered(cast(FleetToolService, stub))) as client:
        await client.call_tool("fleet_spawn", request)
    override = stub.spawn.await_args.kwargs["overrides"]
    assert override.model_fields_set == {"system", "tools"}
    assert override.system is None and override.tools == ()


def test_fleet_does_not_lift_existing_self_only_chat_refusal() -> None:
    with pytest.raises(ToolError, match="chat turn can't open"):
        require_outside_chat_turn(AUTH)


def host_parent() -> WorkerPermissions:
    now = datetime.now(UTC)
    backend = backend_selection("anthropic")
    spec = AgentSpec(
        name="root",
        model=ModelRef(provider="anthropic", id="claude-haiku-5-5"),
        system="Root safety",
        tools=(),
    )
    return WorkerPermissions(
        fleet=FleetFacts(
            scope=Scope(
                tenant_id=str(AUTH.tenant_id),
                account_id=str(AUTH.account_id),
                principal_id="requester",
                authorization_id="host-root",
            ),
            root=ThreadRef(
                channel=ChannelRef(
                    tenant_id=str(AUTH.tenant_id), platform="discord", channel_id="channel"
                ),
                thread_id="thread",
            ),
            origin_id=uuid.UUID(int=9007),
            account_id=AUTH.account_id,
            run_id=RUN,
            place=Place(channel_id="thread", parent_channel_id="channel"),
            enabled=True,
            active=True,
        ),
        subject=Subject(platform_user_id="requester"),
        agent=AgentRef.of("root"),
        spec=spec,
        base_system="Root safety",
        backend_grants=(backend,),
        models=(spec.model,),
        limits=WorkerLimits(
            budget_usd=Decimal("1"), deadline=now + timedelta(minutes=10), max_turns=2
        ),
        agent_id=AUTH.chat_agent_id,
    )


def host_owner() -> Mock:
    owner = create_autospec(FleetPorts, instance=True)
    owner.capture_source.return_value = None
    owner.controller_account.return_value = AUTH.account_id
    owner.read_run.return_value = (
        FleetRun(
            id=RUN,
            tenant_id=AUTH.tenant_id,
            orchestrator_account_id=AUTH.account_id,
            orchestrator_session_id="root-session",
            orchestrator_binding_id="root-binding",
            platform="discord",
            channel_id="channel",
            thread_id="thread",
            status="running",
            total_budget_usd=Decimal("1"),
            reserved_usd=Decimal("0"),
            observed_usd=Decimal("0"),
            deadline_at=datetime.now(UTC) + timedelta(minutes=10),
            max_workers=3,
            quorum=None,
            version=1,
        ),
        (),
    )
    return owner


@pytest.mark.parametrize("tool", sorted(NAMES))
async def test_host_bridge_refuses_missing_authentic_root_loader(tool: str) -> None:
    owner = host_owner()
    bridge = HostFleetToolService(FleetOrchestrator(owner))
    async with Client(registered(bridge)) as client:
        with pytest.raises(ToolError, match="current root is not bound"):
            await client.call_tool(tool, arguments(tool))
    assert owner.mock_calls == []


async def test_host_bridge_reloads_authority_on_each_call_and_refuses_revocation() -> None:
    root = host_parent()
    owner = host_owner()
    loads = []

    async def load(caller: AuthIdentity) -> tuple[WorkerPermissions, TenantAccessPolicy]:
        loads.append(caller)
        return root, TenantAccessPolicy(enabled_backends=("anthropic",))

    bridge = HostFleetToolService(FleetOrchestrator(owner), load)
    async with Client(registered(bridge)) as client:
        first = await client.call_tool("fleet_status", {"run_id": str(RUN)})
        assert first.data["status"] == "running"
        root = replace(root, fleet=replace(root.fleet, active=False))
        with pytest.raises(ToolError, match="worker_spawn_forbidden"):
            await client.call_tool("fleet_status", {"run_id": str(RUN)})
    assert loads == [AUTH, AUTH]
    owner.read_run.assert_awaited_once()


@pytest.mark.parametrize("tool", ("fleet_kill", "fleet_resume", "fleet_collect"))
async def test_mcp_current_caller_cannot_control_another_principals_fleet(tool: str) -> None:
    root = host_parent()
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=root.fleet.scope.model_copy(update={"principal_id": "original-orchestrator"}),
        ),
    )

    async def load(caller: AuthIdentity) -> tuple[WorkerPermissions, TenantAccessPolicy]:
        assert caller is AUTH
        return root, TenantAccessPolicy(enabled_backends=("anthropic",))

    owner = host_owner()
    owner.controller_account.return_value = None
    bridge = HostFleetToolService(FleetOrchestrator(owner), load)
    async with Client(registered(bridge)) as client:
        with pytest.raises(ToolError, match="controller_required"):
            await client.call_tool(tool, arguments(tool))
    owner.read_run.assert_awaited_once()
    owner.retained_control.assert_not_awaited()
    owner.control.assert_not_awaited()
    owner.wait.assert_not_awaited()
    owner.read_result.assert_not_awaited()
    owner.validate_contract.assert_not_called()


@pytest.mark.parametrize("tool", ("fleet_kill", "fleet_resume", "fleet_collect"))
async def test_controller_refusal_persists_as_an_authz_denial(
    tool: str, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, id=AUTH.tenant_id)
        await make_account(session, id=AUTH.account_id, tenant=tenant)
    root = host_parent()
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=root.fleet.scope.model_copy(update={"principal_id": "original-orchestrator"}),
        ),
    )

    async def load(caller: AuthIdentity) -> tuple[WorkerPermissions, TenantAccessPolicy]:
        assert caller is AUTH
        return root, TenantAccessPolicy(enabled_backends=("anthropic",))

    async def subject(_ctx):
        return str(AUTH.account_id)

    async def tenant_claim(_ctx):
        return str(AUTH.tenant_id)

    async def absent(_ctx):
        return None

    async def role(_ctx):
        return "user"

    audit = IdentityMiddleware(
        subject_resolver=subject,
        tenant_resolver=tenant_claim,
        role_resolver=role,
        agent_id_resolver=absent,
        is_admin_resolver=absent,
        internal_resolver=absent,
        sessionmaker=committing_sessionmaker,
    )
    owner = host_owner()
    owner.controller_account.return_value = None
    server = FastMCP("fleet-denial-audit")
    server.add_middleware(audit)
    server.add_middleware(SeedAuth())
    register_fleet_tools(
        server,
        runtime(),
        enabled=True,
        service=HostFleetToolService(FleetOrchestrator(owner), load),
    )
    async with Client(server) as client:
        with pytest.raises(ToolError, match="controller_required"):
            await client.call_tool(tool, arguments(tool))
    await audit.drain_audit()
    async with committing_sessionmaker() as session:
        rows = await list_events(session, tenant_id=AUTH.tenant_id)
    rows = [row for row in rows if row.tool_name == tool]
    assert len(rows) == 1
    assert (rows[0].operation, rows[0].outcome, rows[0].reason) == (
        "fleet_control",
        "denied",
        "authz:controller_required",
    )
    assert rows[0].account_id == AUTH.account_id
    owner.control.assert_not_awaited()
    owner.wait.assert_not_awaited()
    owner.read_result.assert_not_awaited()


@pytest.mark.parametrize(
    "field",
    (
        "tenant",
        "account",
        "platform",
        "subject",
        "admin",
        "channels",
        "depth",
        "agent",
        "agent_key",
    ),
)
async def test_host_bridge_refuses_root_mismatch_or_nested_worker_before_store_reads(
    field: str,
) -> None:
    root = host_parent()
    if field == "tenant":
        root = replace(
            root,
            fleet=replace(
                root.fleet, scope=root.fleet.scope.model_copy(update={"tenant_id": "foreign"})
            ),
        )
    elif field == "account":
        root = replace(root, fleet=replace(root.fleet, account_id=uuid.UUID(int=999)))
    elif field == "platform":
        channel = root.fleet.root.channel.model_copy(update={"platform": "teams"})
        root = replace(
            root,
            fleet=replace(root.fleet, root=root.fleet.root.model_copy(update={"channel": channel})),
        )
    elif field == "subject":
        root = replace(root, subject=replace(root.subject, platform_user_id="other"))
    elif field == "admin":
        root = replace(root, subject=replace(root.subject, is_admin=True))
    elif field == "channels":
        root = replace(
            root, subject=replace(root.subject, administered_channel_ids=frozenset({"extra"}))
        )
    elif field == "depth":
        root = replace(root, fleet=replace(root.fleet, depth=1))
    elif field == "agent":
        root = replace(root, agent_id=uuid.UUID(int=999))
    else:
        root = replace(root, subject=replace(root.subject, via_agent_key=True))

    async def load(caller: AuthIdentity) -> tuple[WorkerPermissions, TenantAccessPolicy]:
        assert caller is AUTH
        return root, TenantAccessPolicy(enabled_backends=("anthropic",))

    owner = host_owner()
    async with Client(registered(HostFleetToolService(FleetOrchestrator(owner), load))) as client:
        with pytest.raises(ToolError, match="caller_root_mismatch|worker_depth_exceeded"):
            await client.call_tool("fleet_status", {"run_id": str(RUN)})
    assert owner.mock_calls == []


@pytest.mark.parametrize("tool", sorted(NAMES))
@pytest.mark.parametrize("credential", ["external_account", "agent_session_token"])
async def test_authenticated_nonroot_credentials_never_reach_fleet_owner(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    credential: str,
) -> None:
    root = host_parent()
    caller = (
        replace(AUTH, is_external=True)
        if credential == "external_account"
        else replace(
            AUTH,
            agent_id=AUTH.chat_agent_id,
        )
    )
    monkeypatch.setitem(globals(), "AUTH", caller)
    loads = []

    async def load(identity: AuthIdentity) -> tuple[WorkerPermissions, TenantAccessPolicy]:
        loads.append(identity)
        return root, TenantAccessPolicy(enabled_backends=("anthropic",))

    owner = host_owner()
    bridge = HostFleetToolService(FleetOrchestrator(owner), load)
    async with Client(registered(bridge)) as client:
        with pytest.raises(ToolError, match="caller_root_mismatch"):
            await client.call_tool(tool, arguments(tool))
    assert loads == [caller]
    assert owner.mock_calls == []


@pytest.mark.parametrize(
    "enabled,path,channel_backends",
    (
        (False, "mux", True),
        (True, "legacy", True),
        (True, "mux", False),
        (True, "mux", True),
    ),
)
async def test_factory_registers_only_for_explicit_fleet_and_neutral_flags(
    enabled: bool,
    path: Literal["legacy", "mux"],
    channel_backends: bool,
) -> None:
    settings = runtime(path=path, channel_backends=channel_backends).settings
    settings.fleet = FleetSettings(enabled=enabled)
    app = create_mcp_app(
        settings=settings,
        sessionmaker=async_sessionmaker(class_=AsyncSession),
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    names = {tool.name for tool in await app.state.mcp.local_provider.list_tools()}
    assert (names & NAMES) == (NAMES if enabled and path == "mux" and channel_backends else set())


async def test_default_factory_does_not_import_or_discover_a_fleet_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        assert name != "daimon.adapters.mcp.tools.fleet", (
            "default factory imported opt-in fleet tools"
        )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    app = create_mcp_app(
        settings=runtime().settings,
        sessionmaker=async_sessionmaker(class_=AsyncSession),
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )
    assert not NAMES & {tool.name for tool in await app.state.mcp.local_provider.list_tools()}


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "foreign",
        "expired",
        "wrong_responder",
        "wrong_thread",
        "wrong_root_origin",
        "valid",
    ],
)
async def test_installed_bridge_requires_the_exact_active_origin(
    db_session_factory, db_clean, fault
):
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.turn_origins import create_origin

    root = host_parent()
    source_id = "native-root"
    caller = replace(
        AUTH, chat_agent_id=derive_agent_uuid(tenant_id=AUTH.tenant_id, ma_agent_id=source_id)
    )
    root = replace(root, agent_id=caller.chat_agent_id)
    now = datetime.now(UTC)
    async with db_session_factory() as db, db.begin():
        tenant = await make_tenant(db, id=AUTH.tenant_id)
        await make_account(db, id=AUTH.account_id, tenant=tenant)
        origin = await create_origin(
            db,
            tenant_id=AUTH.tenant_id,
            account_id=AUTH.account_id,
            platform="discord",
            parent_channel_id="channel",
            thread_id="thread",
            responder_ma_agent_id="other" if fault == "wrong_responder" else source_id,
            responder_name="root",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            expires_at=now + timedelta(minutes=-1 if fault == "expired" else 10),
            now=now,
        )
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            origin_id=uuid.uuid4() if fault == "wrong_root_origin" else origin.id,
            root=root.fleet.root.model_copy(update={"thread_id": "other"})
            if fault == "wrong_thread"
            else root.fleet.root,
        ),
    )
    loads = []

    async def load(caller_, *, origin_context_id, run_id):
        assert caller_ is caller
        loads.append((origin_context_id, run_id))
        return root, TenantAccessPolicy(enabled_backends=("anthropic",))

    owner = host_owner()
    bridge = HostFleetToolService(
        FleetOrchestrator(owner),
        load,
        runtime=replace(runtime(), session_factory=db_session_factory),
    )
    handle = None if fault == "missing" else uuid.uuid4() if fault == "foreign" else origin.id
    if fault == "valid":
        reply = await bridge.status(caller, run_id=RUN, origin_context_id=handle)
        assert reply["status"] == "running"
        assert loads == [(origin.id, RUN)]
        owner.read_run.assert_awaited_once()
    else:
        with pytest.raises(
            ToolError,
            match="caller_root_mismatch"
            if fault in ("wrong_thread", "wrong_root_origin")
            else "active_origin_required",
        ):
            await bridge.status(caller, run_id=RUN, origin_context_id=handle)
        assert owner.mock_calls == []
        assert loads == (
            [(origin.id, RUN)] if fault in ("wrong_thread", "wrong_root_origin") else []
        )


@pytest.mark.parametrize(
    "later_status", ["running", "collecting", "cancelled", "failed", "completed"]
)
async def test_default_bridge_collects_the_original_run_from_a_real_completion_frame(
    root_turn, later_status
):
    from daimon.core.stores import fleet, turn_origins
    from daimon.core.turn.runtime_manifest import read_frozen_root
    from daimon.core.turn_origin import turn_origin

    deps, prepared, grants, _, seed, scope, _, transport = root_turn
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id=seed.parent_channel_id,
        thread_id=seed.thread_id,
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        keep_fleet_root=True,
    ) as origin:
        grants[0] = replace(grants[0], fleet=replace(grants[0].fleet, origin_id=origin.id))
        value = (*root_turn[:4], origin, *root_turn[5:])
        await run_root_input(value)
        original, _ = await read_frozen_root(
            deps,
            scope,
            origin_id=origin.id,
            now=datetime.now(UTC),
        )

    async def no_new_authority(*args):
        raise AssertionError("Completion tools must reuse the original grant")

    deps = replace(deps, fleet_root_grants=no_new_authority)
    value = (deps, *value[1:])
    row, worker = await completion_claim(value)
    ports = HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
    bridge = HostFleetToolService(
        FleetOrchestrator(ports),
        runtime=replace(runtime(), session_factory=deps.sessionmaker, client=deps.anthropic),
        deps=deps,
    )
    caller = replace(
        AUTH,
        tenant_id=origin.tenant_id,
        account_id=origin.account_id,
        chat_agent_id=original.agent_id,
    )
    async with wake_frame(value) as (intent, current):
        await run_root_input(value, intent_id=intent.id, completion=row)
        assert current.id != original.fleet.origin_id
        run, _ = await fleet.run_status(
            deps.sessionmaker, tenant_id=origin.tenant_id, run_id=original.fleet.run_id
        )
        if later_status == "completed":
            run = await fleet.set_run_status(
                deps.sessionmaker,
                tenant_id=run.tenant_id,
                run_id=run.id,
                expected_version=run.version,
                status="collecting",
            )
        if later_status != "running":
            await fleet.set_run_status(
                deps.sessionmaker,
                tenant_id=run.tenant_id,
                run_id=run.id,
                expected_version=run.version,
                status=later_status,
            )
        if later_status in ("cancelled", "failed", "completed"):
            requests = tuple(transport.requests)
            for execution in (origin, current):
                for tool in (bridge.status, bridge.collect):
                    with pytest.raises(ToolError, match="root_unavailable"):
                        await tool(
                            caller,
                            run_id=original.fleet.run_id,
                            origin_context_id=execution.id,
                        )
            assert tuple(transport.requests) == requests
            return
        reply = await bridge.collect(
            caller,
            run_id=original.fleet.run_id,
            origin_context_id=current.id,
        )
        assert reply == {
            "run_id": str(original.fleet.run_id),
            "status": "blocked",
            "reason": "worker_failed",
        }
        status = await bridge.status(
            caller,
            run_id=original.fleet.run_id,
            origin_context_id=current.id,
        )
        assert status["run_id"] == str(original.fleet.run_id)
        with pytest.raises(ToolError, match="root_unavailable"):
            await bridge.collect(caller, run_id=uuid.uuid4(), origin_context_id=current.id)
    with pytest.raises(ToolError, match="active_origin_required"):
        await bridge.collect(
            caller,
            run_id=original.fleet.run_id,
            origin_context_id=current.id,
        )
    async with deps.sessionmaker() as db:
        retained = await turn_origins.get_active_origin(
            db,
            origin_id=origin.id,
            tenant_id=origin.tenant_id,
            account_id=origin.account_id,
            platform="discord",
            now=datetime.now(UTC),
        )
    assert retained is not None
    _, workers = await ports.read_run(original.fleet.scope, original.fleet.run_id)
    assert workers == (worker,) and worker.status == "cancelled"
    assert sum(r.method == "POST" and r.path.endswith("/events") for r in transport.requests) == 2
    assert sum(r.method == "POST" and r.path == "/v1/sessions" for r in transport.requests) == 1
