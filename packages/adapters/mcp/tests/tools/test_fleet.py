"""Fleet adapter contracts over dependency stubs, without execution authority."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from unittest.mock import ANY, AsyncMock, Mock, create_autospec

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
from daimon.testing.db import db_committing_engine as db_committing_engine
from daimon.testing.db import db_committing_pool as db_committing_pool
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.pytest_plugin import db_preflight as db_preflight
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec
from mux.profiles import backend_selection
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.core.tests.turn.test_frozen_root import (
    completion_claim,
    run_root_input,
    wake_frame,
    with_login_tool,
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
    chat_session_id="test-session",
)
NAMES = {
    "fleet_spawn",
    "fleet_status",
    "fleet_kill",
    "fleet_resume",
    "fleet_cross_review",
    "fleet_collect",
    "fleet_agents",
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


def runtime(
    *,
    path: Literal["legacy", "mux"] = "mux",
    channel_backends: bool = True,
    agent_backends: bool = True,
) -> McpRuntime:
    # No SDK constructor or DB engine is needed at this thin adapter boundary.
    return McpRuntime(
        session_factory=cast(Any, None),
        client=cast(Any, None),
        deployment_default=DeploymentDefault(),
        settings=Settings(
            anthropic=AnthropicSettings(api_key=SecretStr("offline")),
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://unused/unused")),
            turn=TurnSettings(
                path=path, channel_backends=channel_backends, agent_backends=agent_backends
            ),
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
    stub.agents.return_value = {"run_id": str(RUN), "agents": []}
    return stub


def arguments(tool: str) -> dict[str, Any]:
    if tool == "fleet_agents":
        return {}
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


@pytest.mark.parametrize(
    "path,channels,agents", [("legacy", True, True), ("mux", False, True), ("mux", True, False)]
)
async def test_existing_neutral_flags_gate_fleet_registration(
    path: Literal["legacy", "mux"], channels: bool, agents: bool
) -> None:
    mcp = FastMCP("off")
    register_fleet_tools(
        mcp, runtime(path=path, channel_backends=channels, agent_backends=agents), enabled=True
    )
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
    with pytest.raises(ToolError, match="chat reply can't open"):
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


@pytest.mark.parametrize("tool", sorted(NAMES))
async def test_an_agent_session_token_never_reaches_fleet_owner(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
) -> None:
    root = host_parent()
    caller = replace(AUTH, agent_id=AUTH.chat_agent_id)
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


async def test_stopped_fleet_factory_does_not_import_or_discover_a_fleet_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fleets are on by default; DAIMON_FLEET__ENABLED=false keeps the tools out entirely."""
    import builtins

    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        assert name != "daimon.adapters.mcp.tools.fleet", (
            "a stopped fleet factory imported the fleet tools"
        )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    app = create_mcp_app(
        settings=runtime().settings.model_copy(update={"fleet": FleetSettings(enabled=False)}),
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
            sender_foreign=False,
            shared_channel=False,
            expires_at=now + timedelta(minutes=-1 if fault == "expired" else 10),
            now=now,
            chat_session_id="test-session",
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


async def test_fleet_tools_say_plainly_why_an_agent_cant_run_a_fleet(root_turn):
    from daimon.core.turn_origin import turn_origin

    deps, prepared, grants, intent, seed, scope, binding, transport = root_turn
    prepared = with_login_tool(prepared)
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
        sender_foreign=False,
        shared_channel=False,
        keep_fleet_root=True,
        chat_session_id="test-session",
    ) as origin:
        # The turn itself runs; only the fleet is off.
        await run_root_input((deps, prepared, grants, intent, origin, scope, binding, transport))
        # The fixture calls MCP after the fake driver returned; supply its
        # current execution grant exactly as the real in-flight call has it.
        from daimon.core.stores.turn_origins import bind_origin_session

        async with deps.sessionmaker.begin() as db:
            await bind_origin_session(db, origin_id=origin.id, chat_session_id=AUTH.chat_session_id)
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
            chat_agent_id=prepared._source.agent_id,
        )
        with pytest.raises(ToolError) as refused:
            await bridge.status(caller, run_id=None, origin_context_id=origin.id)
        assert str(refused.value) == (
            "This agent can't run a fleet yet: it uses a connected tool or repository "
            "with its own login."
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
        sender_foreign=False,
        shared_channel=False,
        keep_fleet_root=True,
        chat_session_id="test-session",
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
        # The test invokes MCP after the fake driver returns. Supply the
        # current execution grant that the real prepared driver holds while
        # an MCP call is in flight; retained root origins remain ungranted.
        async with deps.sessionmaker.begin() as db:
            await turn_origins.bind_origin_session(
                db, origin_id=current.id, chat_session_id=caller.chat_session_id
            )
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
                    with pytest.raises(
                        ToolError,
                        match=(
                            "root_unavailable"
                            if execution.id == current.id
                            else "active_origin_required"
                        ),
                    ):
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


@pytest.mark.parametrize("tool", ["fleet_spawn", "fleet_cross_review"])
@pytest.mark.parametrize("caps", [None, (262144, 65536)])
async def test_public_token_caps_reach_existing_worker_limits(tool, caps) -> None:
    root = host_parent()
    orchestrator = fleet_orchestrator_double()
    orchestrator.spawn.return_value = {"run_id": str(RUN), "status": "pending"}

    async def load(caller):
        assert caller is AUTH
        return root, TenantAccessPolicy(enabled_backends=("anthropic",))

    request = arguments(tool)
    if caps is not None:
        request.update(max_input_tokens=caps[0], max_output_tokens=caps[1])
    async with Client(registered(HostFleetToolService(orchestrator, load))) as client:
        await client.call_tool(tool, request)
    limits = orchestrator.spawn.await_args.kwargs["limits"]
    assert isinstance(limits, WorkerLimits)
    assert limits.budget_usd == Decimal(request["budget_usd"])
    assert limits.max_input_tokens == (caps[0] if caps else None)
    assert limits.max_output_tokens == (caps[1] if caps else None)
    if caps is None:
        assert not {"max_input_tokens", "max_output_tokens"} & limits.model_dump().keys()


@pytest.mark.parametrize("tool", ["fleet_spawn", "fleet_cross_review"])
@pytest.mark.parametrize("field", ["max_input_tokens", "max_output_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "262144"])
async def test_public_token_caps_reject_invalid_shapes_before_owner(tool, field, value) -> None:
    stub = service()
    request = arguments(tool)
    request[field] = value
    async with Client(registered(cast(FleetToolService, stub))) as client:
        with pytest.raises(ToolError):
            await client.call_tool(tool, request)
    stub.spawn.assert_not_awaited()


@pytest.mark.parametrize("fault", ["expanded", "unsupported", "incomplete"])
async def test_public_token_caps_keep_existing_resolver_refusals(fault) -> None:
    root = host_parent()
    selected = backend_selection("gemini")
    model = ModelRef(provider="gemini", id=selected.model)
    root = replace(
        root,
        spec=root.spec.model_copy(update={"model": model}),
        backend_grants=(selected,),
        models=(model,),
        limits=root.limits.model_copy(
            update={"max_input_tokens": 262144, "max_output_tokens": 65536}
        ),
    )
    if fault == "unsupported":
        root = host_parent()

    async def load(caller):
        return root, TenantAccessPolicy(enabled_backends=("anthropic", "gemini"))

    request = arguments("fleet_spawn")
    request.update(
        backend=root.backend_grants[0].model_dump(mode="json"),
        agent=root.spec.model_dump(mode="json"),
        max_input_tokens=262145 if fault == "expanded" else 262144,
        max_output_tokens=None if fault == "incomplete" else 65536,
        max_turns=1,
    )
    owner = host_owner()
    async with Client(registered(HostFleetToolService(FleetOrchestrator(owner), load))) as client:
        with pytest.raises(
            ToolError, match="outside what this fleet allows|isn.t supported for this worker"
        ):
            await client.call_tool("fleet_spawn", request)
    owner.admit.assert_not_awaited()


@pytest.mark.parametrize(
    "field",
    ("tenant", "account", "platform", "subject", "admin", "depth", "agent", "agent_key"),
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


def fleet_orchestrator_double() -> Mock:
    """Mocked owner operations; spawn limits still run the real defaulting code."""
    orchestrator = create_autospec(FleetOrchestrator, instance=True)
    orchestrator.admitted_worker.return_value = None

    async def spawn_limits(*args: Any, **kwargs: Any) -> WorkerLimits:
        return await FleetOrchestrator.spawn_limits(orchestrator, *args, **kwargs)

    orchestrator.spawn_limits.side_effect = spawn_limits
    return orchestrator


def plain_root() -> WorkerPermissions:
    root = host_parent()
    luna = ModelRef(provider="openai", id="gpt-6-luna")
    return replace(
        root,
        backend_grants=(*root.backend_grants, backend_selection("openai")),
        models=(*root.models, luna),
    )


def plain_bridge(root: WorkerPermissions) -> tuple[HostFleetToolService, Mock]:
    orchestrator = fleet_orchestrator_double()
    orchestrator.spawn.return_value = {"run_id": str(RUN), "status": "pending"}
    orchestrator.read_snapshot.return_value = host_owner().read_run.return_value
    orchestrator.collect.return_value = {"run_id": str(RUN), "status": "waiting"}

    async def load(caller):
        assert caller is AUTH
        return root, TenantAccessPolicy(enabled_backends=("anthropic", "openai"))

    return HostFleetToolService(orchestrator, load), orchestrator


@pytest.mark.parametrize("model", [None, "gpt-6-luna"])
async def test_fleet_r3_r4_plain_spawn_needs_only_role_task_and_model(model) -> None:
    """r3 invented a run_id and r4 a too-late deadline; neither is needed now."""
    root = plain_root()
    bridge, orchestrator = plain_bridge(root)
    request = {"operation_key": "W1", "role": "W1", "task": "Compare X and Y."}
    if model is not None:
        request["model"] = model
    async with Client(registered(bridge)) as client:
        await client.call_tool("fleet_spawn", request)
        await client.call_tool(
            "fleet_spawn",
            {**request, "deadline": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        )
    first, later = orchestrator.spawn.await_args_list
    kwargs = first.kwargs
    assert kwargs["run_id"] is None and kwargs["backend"] is None and kwargs["clone"] is None
    agent = kwargs["agent"]
    assert agent.model == (root.spec.model if model is None else root.models[1])
    assert agent.system == "Compare X and Y."
    assert agent.declared_drops == (
        ("skills", "tools") if model is None else ("mcp", "memory", "sandbox", "skills", "tools")
    )
    limits = kwargs["limits"]
    # Run budget $1 over 3 worker slots; the run's deadline; one turn.
    assert limits.budget_usd == Decimal("0.33") and limits.max_turns == 1
    assert limits.deadline == root.limits.deadline
    assert later.kwargs["limits"].deadline == root.limits.deadline
    orchestrator.read_snapshot.assert_awaited_with(root, ANY, RUN)


async def test_plain_spawn_refuses_ungranted_models_and_mixed_specs() -> None:
    bridge, orchestrator = plain_bridge(plain_root())
    request = {"operation_key": "W1", "role": "W1", "task": "Compare X and Y."}
    async with Client(registered(bridge)) as client:
        with pytest.raises(ToolError, match="isn.t allowed in this fleet"):
            await client.call_tool("fleet_spawn", {**request, "model": "gemini-3.8-pro"})
        with pytest.raises(ToolError, match="exactly one"):
            await client.call_tool("fleet_spawn", {**request, **arguments("fleet_spawn")})
        with pytest.raises(ToolError, match="goes with a task"):
            await client.call_tool(
                "fleet_spawn", {**arguments("fleet_spawn"), "model": "gpt-6-luna"}
            )
    orchestrator.spawn.assert_not_awaited()


async def test_explicit_budget_skips_the_run_read_and_reaches_the_resolver() -> None:
    bridge, orchestrator = plain_bridge(plain_root())
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn",
            {"operation_key": "W1", "role": "W1", "task": "Go.", "budget_usd": "0.90"},
        )
    assert orchestrator.spawn.await_args.kwargs["limits"].budget_usd == Decimal("0.90")
    orchestrator.read_snapshot.assert_not_awaited()


@pytest.mark.parametrize("tool", ["fleet_status", "fleet_collect"])
async def test_status_and_collect_default_to_this_turns_run(tool) -> None:
    bridge, orchestrator = plain_bridge(plain_root())
    orchestrator.status.return_value = {"run_id": str(RUN), "status": "running"}
    async with Client(registered(bridge)) as client:
        result = await client.call_tool(tool, {})
    assert result.data["run_id"] == str(RUN)
    assert getattr(orchestrator, tool.removeprefix("fleet_")).await_args.kwargs["run_id"] == RUN


async def test_plain_spawn_with_no_budget_left_refuses_before_admission() -> None:
    bridge, orchestrator = plain_bridge(plain_root())
    run, _ = orchestrator.read_snapshot.return_value
    orchestrator.read_snapshot.return_value = (replace(run, total_budget_usd=Decimal("0.02")), ())
    async with Client(registered(bridge)) as client:
        with pytest.raises(ToolError, match="budget is used up"):
            await client.call_tool(
                "fleet_spawn", {"operation_key": "W1", "role": "W1", "task": "Compare X and Y."}
            )
    orchestrator.spawn.assert_not_awaited()


async def test_planned_workers_hint_reaches_the_default_budget() -> None:
    bridge, orchestrator = plain_bridge(plain_root())
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn",
            {"operation_key": "W1", "role": "W1", "task": "Compare X and Y.", "planned_workers": 2},
        )
        with pytest.raises(ToolError):
            await client.call_tool(
                "fleet_spawn",
                {"operation_key": "W2", "role": "W2", "task": "Go.", "planned_workers": 0},
            )
    # Run budget $1 shared by the 2 planned workers instead of 3 slots.
    assert orchestrator.spawn.await_args.kwargs["limits"].budget_usd == Decimal("0.50")
    orchestrator.spawn.assert_awaited_once()


async def test_fleet_r7_plain_gemini_worker_gets_the_gemini_runtime_bound() -> None:
    root = plain_root()
    flash = ModelRef(provider="gemini", id="gemini-3.8-flash")
    root = replace(
        root,
        backend_grants=(
            *root.backend_grants,
            backend_selection("gemini").model_copy(update={"model": flash.id}),
        ),
        models=(*root.models, flash),
        limits=root.limits.model_copy(
            update={"deadline": datetime.now(UTC) + timedelta(minutes=20)}
        ),
    )
    bridge, orchestrator = plain_bridge(root)
    before = datetime.now(UTC)
    async with Client(registered(bridge)) as client:
        for model in ("gemini-3.8-flash", "gpt-6-luna"):
            await client.call_tool(
                "fleet_spawn",
                {"operation_key": model, "role": "W", "task": "Compare.", "model": model},
            )
    gemini, luna = (call.kwargs["limits"] for call in orchestrator.spawn.await_args_list)
    assert before + timedelta(minutes=10) <= gemini.deadline
    assert gemini.deadline <= datetime.now(UTC) + timedelta(minutes=10)
    assert luna.deadline == root.limits.deadline


async def test_the_grants_worker_time_bounds_every_spawned_worker() -> None:
    """ROOT 10-min decision: each worker gets the grant's worker time, on any provider."""
    root = replace(plain_root(), max_worker_seconds=300)
    bridge, orchestrator = plain_bridge(root)
    before = datetime.now(UTC)
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn",
            {"operation_key": "luna", "role": "W", "task": "Compare.", "model": "gpt-6-luna"},
        )
    (luna,) = (call.kwargs["limits"] for call in orchestrator.spawn.await_args_list)
    assert root.limits.deadline > before + timedelta(seconds=300)
    assert before + timedelta(seconds=300) <= luna.deadline
    assert luna.deadline <= datetime.now(UTC) + timedelta(seconds=300)


async def test_plain_spawn_retry_reuses_the_admitted_workers_limits() -> None:
    from types import SimpleNamespace

    root = plain_root()
    bridge, orchestrator = plain_bridge(root)
    stored = SimpleNamespace(
        run_id=RUN,
        budget_usd=Decimal("0.330000"),
        deadline_at=datetime.now(UTC) + timedelta(seconds=40),
    )
    orchestrator.admitted_worker.return_value = stored
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn", {"operation_key": "W1", "role": "W1", "task": "Compare X and Y."}
        )
    limits = orchestrator.spawn.await_args.kwargs["limits"]
    assert (limits.budget_usd, limits.deadline) == (Decimal("0.33"), stored.deadline_at)
    orchestrator.read_snapshot.assert_not_awaited()


async def test_fleet_r9_token_limits_are_filled_only_when_the_caller_gives_none() -> None:
    bridge, orchestrator = plain_bridge(plain_root())
    async with Client(registered(bridge)) as client:
        await client.call_tool("fleet_spawn", {"operation_key": "W1", "role": "W", "task": "Go."})
        await client.call_tool(
            "fleet_spawn",
            {
                "operation_key": "W2",
                "role": "W",
                "task": "Go.",
                "max_input_tokens": 262144,
                "max_output_tokens": 65536,
            },
        )
    plain, explicit = orchestrator.spawn.await_args_list
    assert plain.kwargs["fill_token_limits"] is True
    assert explicit.kwargs["fill_token_limits"] is False
    assert explicit.kwargs["limits"].max_input_tokens == 262144


async def test_planned_models_give_each_provider_its_own_share() -> None:
    root = plain_root()
    root = replace(
        root,
        limits=root.limits.model_copy(
            update={"provider_limits_usd": {"anthropic": Decimal("0.5"), "openai": Decimal("0.5")}}
        ),
    )
    bridge, orchestrator = plain_bridge(root)
    plan = ["gpt-6-luna", "claude-haiku-5-5"]
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn",
            {
                "operation_key": "W1",
                "role": "W1",
                "task": "Compare X and Y.",
                "model": "gpt-6-luna",
                "planned_models": plan,
            },
        )
        with pytest.raises(ToolError, match='"gemini-3.8-pro" isn.t allowed in this fleet'):
            await client.call_tool(
                "fleet_spawn",
                {
                    "operation_key": "W2",
                    "role": "W2",
                    "task": "Go.",
                    "planned_models": [*plan, "gemini-3.8-pro"],
                },
            )
        with pytest.raises(ToolError, match="planned_workers is smaller"):
            await client.call_tool(
                "fleet_spawn",
                {
                    "operation_key": "W3",
                    "role": "W3",
                    "task": "Go.",
                    "planned_models": plan,
                    "planned_workers": 1,
                },
            )
    # Run $1 over the 2 planned workers; the OpenAI line ($0.50) is not split.
    assert orchestrator.spawn.await_args.kwargs["limits"].budget_usd == Decimal("0.50")
    orchestrator.spawn.assert_awaited_once()


async def test_a_named_agent_worker_spawns_as_that_agent_with_its_own_defaults() -> None:
    root = plain_root()
    luna = ModelRef(provider="openai", id="gpt-6-luna")
    researcher = replace(
        root,
        spec=root.spec.model_copy(update={"name": "researcher", "model": luna}),
        agent_id=uuid.UUID(int=4242),
    )
    bridge, orchestrator = plain_bridge(root)
    orchestrator.agent_worker.return_value = researcher
    orchestrator.worker_agents.return_value = [{"name": "researcher", "model": "gpt-6-luna"}]
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn",
            {"operation_key": "R1", "role": "research", "agent": "researcher", "task": "Dig in."},
        )
        listed = await client.call_tool("fleet_agents", {})
        for bad in ({"model": "gpt-6-luna"}, {"clone": {"kind": "agent", "id": "x"}}):
            with pytest.raises(ToolError):
                await client.call_tool(
                    "fleet_spawn",
                    {"operation_key": "R2", "role": "r", "agent": "researcher", **bad},
                )
    assert orchestrator.agent_worker.await_args.kwargs["agent_name"] == "researcher"
    kwargs = orchestrator.spawn.await_args.kwargs
    assert (kwargs["as_agent"], kwargs["task"], kwargs["agent"]) == ("researcher", "Dig in.", None)
    # The run budget's default share uses the agent's own provider line.
    assert kwargs["limits"].budget_usd == Decimal("0.33")
    assert listed.structured_content["agents"] == [{"name": "researcher", "model": "gpt-6-luna"}]


async def test_a_fleet_plan_may_name_agent_workers_models_the_grant_does_not_list() -> None:
    root = plain_root()
    luna = ModelRef(provider="openai", id="gpt-6-luna")
    researcher = replace(
        root,
        spec=root.spec.model_copy(update={"name": "researcher", "model": luna}),
        models=(luna,),
        agent_id=uuid.UUID(int=4242),
    )
    assert "gemini-3.8-flash" not in {model.id for model in root.models}
    bridge, orchestrator = plain_bridge(root)
    orchestrator.agent_worker.return_value = researcher
    orchestrator.worker_agents.return_value = [
        {"name": "researcher", "backend": "openai", "model": "gpt-6-luna"},
        {"name": "flash", "backend": "gemini", "model": "gemini-3.8-flash"},
    ]
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn",
            {
                "operation_key": "P1",
                "role": "research",
                "agent": "researcher",
                "task": "Dig in.",
                "planned_models": ["gpt-6-luna", "gemini-3.8-flash"],
            },
        )
        with pytest.raises(ToolError, match='"gpt-6-nova" isn.t allowed in this fleet'):
            await client.call_tool(
                "fleet_spawn",
                {
                    "operation_key": "P2",
                    "role": "research",
                    "agent": "researcher",
                    "task": "Dig in.",
                    "planned_models": ["gpt-6-nova"],
                },
            )
    orchestrator.spawn.assert_awaited_once()


async def test_an_agent_worker_can_declare_its_own_losses() -> None:
    root = plain_root()
    researcher = replace(root, agent_id=uuid.UUID(int=4242))
    bridge, orchestrator = plain_bridge(root)
    orchestrator.agent_worker.return_value = researcher
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn",
            {
                "operation_key": "R1",
                "role": "research",
                "agent": {"name": "researcher", "declared_drops": ["tools", "skills"]},
                "task": "Dig in.",
            },
        )
        # Only whole-capability losses, and nothing but a name and losses.
        for bad in (
            {"name": "researcher", "declared_drops": ["mcp_tool:a:b"]},
            {"name": "researcher", "tools": []},
            {"name": "r" * 201},
        ):
            with pytest.raises(ToolError):
                await client.call_tool(
                    "fleet_spawn",
                    {"operation_key": "R2", "role": "r", "agent": bad, "task": "x"},
                )
    kwargs = orchestrator.spawn.await_args.kwargs
    assert kwargs["as_agent"] == "researcher" and kwargs["agent"] is None
    assert kwargs["as_agent_drops"] == ("tools", "skills")
    orchestrator.spawn.assert_awaited_once()


async def test_a_worker_running_as_another_agent_cannot_start_a_fleet() -> None:
    """Its token names that agent, never the root's, so fleet tools refuse it."""
    root = replace(host_parent(), agent_id=uuid.UUID(int=4242))
    owner = host_owner()

    async def load(caller):
        return root, TenantAccessPolicy(enabled_backends=("anthropic",))

    async with Client(registered(HostFleetToolService(FleetOrchestrator(owner), load))) as client:
        with pytest.raises(ToolError, match="caller_root_mismatch"):
            await client.call_tool(
                "fleet_spawn",
                {"operation_key": "N1", "role": "nested", "agent": "researcher", "task": "Go."},
            )
    owner.agent_worker_parent.assert_not_awaited()


def fork_root() -> WorkerPermissions:
    """daimon-fleet on staging: a Sonnet root whose grant allows Opus, Astra and Argon."""
    root = host_parent()
    sonnet = ModelRef(provider="anthropic", id="claude-sonnet-5-5")
    granted = (
        sonnet,
        ModelRef(provider="anthropic", id="claude-opus-5-5"),
        ModelRef(provider="openai", id="gpt-6-astra"),
        ModelRef(provider="gemini", id="gemini-4-argon"),
    )
    return replace(
        root,
        spec=root.spec.model_copy(update={"model": sonnet}),
        backend_grants=(
            *root.backend_grants,
            backend_selection("openai").model_copy(update={"model": "gpt-6-astra"}),
            backend_selection("gemini").model_copy(update={"model": "gemini-4-argon"}),
        ),
        models=granted,
    )


async def test_fork_yourself_on_opus_astra_and_argon_resolves_each_plain_name() -> None:
    """J3: 'fork yourself into 3 workers on Opus 5.5, Astra and Argon' spawned nothing."""
    bridge, orchestrator = plain_bridge(fork_root())
    # The names the root passed in the failed run, then the user's own phrasing.
    for plan, names in (
        (["claude-opus-5-5", "astra", "argon"], ["claude-opus-5-5", "astra", "argon"]),
        (["Opus 5.5", "Astra", "Argon"], ["Opus 5.5", "Astra", "Argon"]),
    ):
        orchestrator.spawn.reset_mock()
        async with Client(registered(bridge)) as client:
            for index, name in enumerate(names, start=1):
                await client.call_tool(
                    "fleet_spawn",
                    {
                        "operation_key": f"W{index}",
                        "role": f"W{index}",
                        "task": "Postgres vs SQLite for a small team's internal tool?",
                        "model": name,
                        "planned_models": plan,
                    },
                )
        spawned = [call.kwargs["agent"].model for call in orchestrator.spawn.await_args_list]
        assert spawned == [
            ModelRef(provider="anthropic", id="claude-opus-5-5"),
            ModelRef(provider="openai", id="gpt-6-astra"),
            ModelRef(provider="gemini", id="gemini-4-argon"),
        ]
        # The plan splits the budget by provider: one worker each.
        limits = orchestrator.spawn_limits.await_args_list[-3:]
        assert [call.kwargs["planned_on_provider"] for call in limits] == [1, 1, 1]
        assert all(call.kwargs["planned_workers"] == 3 for call in limits)


async def test_a_fork_to_another_granted_model_of_the_roots_provider_is_not_a_backend_refusal() -> (
    None
):
    """J3 (b): Opus from a Sonnet root failed only because the plan named ungranted names."""
    bridge, orchestrator = plain_bridge(fork_root())
    async with Client(registered(bridge)) as client:
        await client.call_tool(
            "fleet_spawn",
            {
                "operation_key": "W1",
                "role": "W1",
                "task": "Go.",
                "model": "claude-opus-5-5",
                "planned_models": ["claude-opus-5-5", "gpt-6-astra", "gemini-4-argon"],
            },
        )
    (call,) = orchestrator.spawn.await_args_list
    assert call.kwargs["agent"].model == ModelRef(provider="anthropic", id="claude-opus-5-5")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model", "sol", '"sol" isn\'t allowed in this fleet; allowed: '),
        ("planned_models", ["astra", "nova"], '"nova" isn\'t allowed in this fleet; allowed: '),
        ("model", "claude", '"claude" could mean '),
    ],
)
async def test_unknown_or_ambiguous_worker_model_names_get_a_plain_refusal_with_the_allowed_list(
    field: str, value: object, message: str
) -> None:
    from structlog.testing import capture_logs

    bridge, orchestrator = plain_bridge(fork_root())
    request: dict[str, object] = {"operation_key": "W1", "role": "W1", "task": "Go.", field: value}
    async with Client(registered(bridge)) as client:
        with capture_logs() as logs, pytest.raises(ToolError) as refused:
            await client.call_tool("fleet_spawn", request)
    text = str(refused.value)
    assert message in text and "not_subset" not in text
    assert "GPT-6 Astra (gpt-6-astra)" in text and "Gemini 4 Argon (gemini-4-argon)" in text
    orchestrator.spawn.assert_not_awaited()
    (entry,) = [e for e in logs if e["event"] == "fleet.spawn_refused"]
    assert entry["category"] == "request" and entry["operation_key"] == "W1"


async def test_core_refusals_reach_the_root_as_plain_text_and_are_logged_by_category() -> None:
    from daimon.core.fleet_orchestration import FleetRefused
    from daimon.core.neutral_resources import WorkerSpecRefused
    from structlog.testing import capture_logs

    bridge, orchestrator = plain_bridge(fork_root())
    request = {"operation_key": "W1", "role": "W1", "task": "Go.", "model": "astra"}
    cases = [
        (
            WorkerSpecRefused("backend", "not_subset"),
            "The worker's model or model provider is outside",
            "worker_spec",
        ),
        (
            WorkerSpecRefused("budget", "exhausted"),
            "The worker's budget is used up.",
            "worker_spec",
        ),
        (
            FleetRefused("run_limit_reached"),
            "Fleet refused this worker: run limit reached.",
            "fleet",
        ),
        # J7: the real limits and the user's fix reach the root, not just the code.
        (
            FleetRefused(
                "over_fleet_limits", detail="This fleet is limited to 4 workers and $2 a run."
            ),
            "Fleet refused this worker: over fleet limits. This fleet is limited to 4 workers "
            "and $2 a run. Ask for at most that many workers.",
            "fleet",
        ),
        (
            FleetRefused("financial_admission_refused"),
            "Use fewer or cheaper workers, or ask the agent's owner or an admin to raise the "
            "fleet budget.",
            "fleet",
        ),
    ]
    async with Client(registered(bridge)) as client:
        for error, text, category in cases:
            orchestrator.spawn.side_effect = error
            with capture_logs() as logs, pytest.raises(ToolError) as refused:
                await client.call_tool("fleet_spawn", request)
            assert text in str(refused.value) and "_" not in str(refused.value).split(":")[-1]
            (entry,) = [e for e in logs if e["event"] == "fleet.spawn_refused"]
            assert entry["category"] == category


async def test_fleet_agents_lists_the_models_a_task_worker_may_run_on_by_plain_name() -> None:
    bridge, orchestrator = plain_bridge(fork_root())
    orchestrator.worker_agents.return_value = [{"name": "researcher", "model": "gpt-6-luna"}]
    async with Client(registered(bridge)) as client:
        listed = await client.call_tool("fleet_agents", {})
    content = listed.structured_content
    assert content["agents"] == [{"name": "researcher", "model": "gpt-6-luna"}]
    assert content["models"] == [
        {"name": "Claude Sonnet 5.5", "id": "claude-sonnet-5-5", "provider": "anthropic"},
        {"name": "Claude Opus 5.5", "id": "claude-opus-5-5", "provider": "anthropic"},
        {"name": "GPT-6 Astra", "id": "gpt-6-astra", "provider": "openai"},
        {"name": "Gemini 4 Argon", "id": "gemini-4-argon", "provider": "gemini"},
    ]


async def test_the_local_opus_fast_preset_is_never_a_fleet_worker_model() -> None:
    """Review: claude-opus-5-5-fast is a local preset only the legacy path expands."""
    root = fork_root()
    fast = ModelRef(provider="anthropic", id="claude-opus-5-5-fast")
    bridge, orchestrator = plain_bridge(replace(root, models=(*root.models, fast)))
    async with Client(registered(bridge)) as client:
        # Every spelling of the preset, including the long forms close-matching would
        # otherwise turn into standard Opus, is refused before resolution.
        for name in ("opus fast", "claude-opus-5-5-fast", "Claude Opus 5.5 fast"):
            with pytest.raises(ToolError) as refused:
                await client.call_tool(
                    "fleet_spawn",
                    {"operation_key": "W1", "role": "W1", "task": "Go.", "model": name},
                )
            assert "can't run as a fleet worker" in str(refused.value)
            assert "(claude-opus-5-5-fast)" not in str(refused.value)
        listed = await client.call_tool("fleet_agents", {})
        assert "claude-opus-5-5-fast" not in {m["id"] for m in listed.structured_content["models"]}
        await client.call_tool(
            "fleet_spawn", {"operation_key": "W2", "role": "W2", "task": "Go.", "model": "opus"}
        )
    (call,) = orchestrator.spawn.await_args_list
    assert call.kwargs["agent"].model == ModelRef(provider="anthropic", id="claude-opus-5-5")


@pytest.mark.parametrize("custom", [False, True])
async def test_a_slack_root_with_no_fleet_is_told_fleets_arent_available_here(root_turn, custom):
    """No default fleet where no adapter settles and delivers workers (Slack, Teams).

    It never names another chat app. A custom grant's root that went stale is still
    just unavailable, not "not available here".
    """
    from daimon.core.turn_origin import turn_origin
    from sqlalchemy import text

    deps, prepared, _, _, seed, _, _, _ = root_turn
    if not custom:
        # No stored fleet setting at all: the agent has only the computed default.
        async with deps.sessionmaker.begin() as db:
            await db.execute(text("DELETE FROM agent_backend_revision"))
    bridge = HostFleetToolService(
        FleetOrchestrator(
            HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
        ),
        runtime=replace(runtime(), session_factory=deps.sessionmaker, client=deps.anthropic),
        deps=deps,
    )
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="slack",
        parent_channel_id="C-slack",
        thread_id="T-slack",
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        keep_fleet_root=True,
        chat_session_id="test-session",
    ) as slack_origin:
        from daimon.core.ma_identity import derive_agent_uuid

        caller = replace(
            AUTH,
            tenant_id=slack_origin.tenant_id,
            account_id=slack_origin.account_id,
            platform="slack",
            chat_agent_id=derive_agent_uuid(
                tenant_id=slack_origin.tenant_id, ma_agent_id=prepared.admission.agent.id
            ),
        )
        with pytest.raises(ToolError) as refused:
            await bridge.status(caller, run_id=None, origin_context_id=slack_origin.id)
        message = str(refused.value)
        assert "Discord" not in message
        if custom:
            assert "root_unavailable" in message
        else:
            assert message == "Fleets aren't available here yet."


@pytest.mark.parametrize("same_agent", [True, False])
async def test_a_root_refused_in_another_process_is_explained_by_its_own_setup(
    root_turn, monkeypatch, same_agent
):
    """J4 (staging 09:2xZ): the chat adapter refused the root (obstacle=credentials) in its
    own process; the MCP fleet tools must still say why, not root_unavailable."""
    from daimon.adapters.mcp.tools import fleet_config
    from daimon.core import channel_backend
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.domain import BackendAgent
    from daimon.core.turn_origin import turn_origin

    deps, prepared, _, _, seed, _, _, _ = root_turn
    agent_id = derive_agent_uuid(tenant_id=seed.tenant_id, ma_agent_id=prepared.admission.agent.id)
    looked_up = BackendAgent(
        tenant_id=seed.tenant_id,
        agent_id=agent_id if same_agent else uuid.uuid4(),
        name=prepared.admission.agent.name,
        configuration_digest="c",
    )

    async def load(client, sessions, *, tenant_id, agent_name, **kwargs):
        assert agent_name == prepared.admission.agent.name
        return looked_up

    async def obstacle(runtime, auth, agent):
        assert agent is looked_up
        return "credentials"

    monkeypatch.setattr(channel_backend, "load_backend_agent", load)
    monkeypatch.setattr(fleet_config, "fleet_root_obstacle_for", obstacle)
    bridge = HostFleetToolService(
        FleetOrchestrator(
            HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
        ),
        runtime=replace(runtime(), session_factory=deps.sessionmaker, client=deps.anthropic),
        deps=deps,
    )
    async with turn_origin(
        deps.sessionmaker,
        tenant_id=seed.tenant_id,
        account_id=seed.account_id,
        platform="discord",
        parent_channel_id="fresh-channel",
        thread_id="fresh-thread",
        responder_ma_agent_id=prepared.admission.agent.id,
        responder_name=prepared.admission.agent.name,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        keep_fleet_root=True,
        chat_session_id="test-session",
    ) as fresh:
        caller = replace(
            AUTH,
            tenant_id=fresh.tenant_id,
            account_id=fresh.account_id,
            platform="discord",
            chat_agent_id=agent_id,
        )
        with pytest.raises(ToolError) as refused:
            await bridge.status(caller, run_id=None, origin_context_id=fresh.id)
    if same_agent:
        assert str(refused.value) == (
            "This agent can't run a fleet yet: it uses a connected tool or repository "
            "with its own login."
        )
    else:
        assert "root_unavailable" in str(refused.value)


async def test_a_model_the_person_named_that_isnt_allowed_is_asked_about_not_substituted() -> None:
    """J7m: "fork yourself onto Gemini Pro" ran a paid worker on Argon, unasked."""
    from daimon.adapters.mcp.tools.fleet import ASK_DONT_SUBSTITUTE

    bridge, orchestrator = plain_bridge(fork_root())
    async with Client(registered(bridge)) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        for name in ("fleet_spawn", "fleet_agents"):
            guidance = " ".join((tools[name].description or "").split())
            assert "ask which" in guidance and "isn't available" in guidance
        assert "Never run a paid worker on a substitute" in " ".join(
            (tools["fleet_spawn"].description or "").split()
        )
        for model in ("gemini pro", "opus fast", "claude"):
            with pytest.raises(ToolError) as refused:
                await client.call_tool(
                    "fleet_spawn",
                    {"operation_key": "W1", "role": "W1", "task": "Go.", "model": model},
                )
            assert ASK_DONT_SUBSTITUTE in str(refused.value)
    orchestrator.spawn.assert_not_awaited()


async def test_j10_fork_yourself_across_models_means_fleet_workers_not_a_saved_copy() -> None:
    """J10: "fork yourself across opus, astra and argon" got "Forking is admin-only"."""
    from pathlib import Path

    from daimon.adapters.mcp.tools.agents import register_agent_tools
    from daimon.adapters.mcp.tools.fleet import fleet_model

    phrase = "fork yourself across Opus 5.5, Astra and Argon"
    bridge, _ = plain_bridge(fork_root())
    async with Client(registered(bridge)) as client:
        spawn = next(t for t in await client.list_tools() if t.name == "fleet_spawn")
    spawn_doc = " ".join((spawn.description or "").split())
    assert '"fork yourself across Opus 5.5, Astra and Argon"' in spawn_doc
    assert "No admin is needed and no saved agent is made" in spawn_doc
    agents = FastMCP("agents")
    register_agent_tools(agents, runtime())
    fork = next(t for t in await agents.list_tools() if t.name == "fork_agent")
    fork_doc = " ".join((fork.description or "").split())
    assert "Only for a saved, named copy" in fork_doc and "fleet_spawn" in fork_doc
    skill = (Path(__file__).parents[5] / "defaults/skills/workspace-setup/SKILL.md").read_text()
    skill = " ".join(skill.split())
    assert '"Fork yourself across Opus, Astra and Argon"' in skill and "`fleet_spawn`" in skill
    assert "Forking is admin-only" not in skill
    # And the models it names resolve to the fleet's allowed ids.
    names = phrase.split("across ")[1].replace(" and ", ", ").split(", ")
    allowed = fork_root().models
    assert [fleet_model(name, allowed) for name in names] == [
        "claude-opus-5-5",
        "gpt-6-astra",
        "gemini-4-argon",
    ]
