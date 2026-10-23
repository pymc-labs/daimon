"""Admin ceilings meet actual captured source through the resource owner."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.authz import FleetFacts, Place, Subject, Surface, build_agent_ref
from daimon.core.channel_backend import load_backend_agent
from daimon.core.mux_compat import retrieve_agent
from daimon.core.neutral_resources import (
    AgentFleetGrant,
    WorkerLimits,
    WorkerSpecRefused,
    fleet_root_obstacle,
    fleet_root_permissions,
    resolve_worker_spec,
)
from daimon.core.sessions import project_session_resources
from daimon.core.stores import agent_memory_stores
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.turn.admission import AdmissionGrant
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import MCPConnection, ResourceBinding, ToolSpec
from mux.profiles import backend_selection
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned

NOW = datetime(2026, 10, 13, 23, tzinfo=UTC)


@pytest.fixture
async def captured(owned, request):
    service, identity, actor, sessions = owned
    await enable(service, identity, actor)
    scope = Scope(
        tenant_id=str(identity.tenant_id),
        account_id=str(actor.account_id),
        principal_id="admitted-caller",
        authorization_id="admitted-root",
    )
    live = getattr(request, "param", None) in (
        "haiku55",
        "haiku55_credentials",
        "haiku55_effort",
        "sonnet55_effort",
    )
    capture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/anthropic_live_root.json").read_text()
    )
    native = ma_agent(
        id="root-source",
        name="helper",
        tenant_id=identity.tenant_id,
        model="claude-haiku-5-5",
        system="Protected source instruction",
        version=10 if live else 1,
        tools=capture["tools"]
        if live
        else [
            {
                "type": "custom",
                "name": "helper",
                "description": "Read facts",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        skills=capture["skills"] if live else (),
        mcp_servers=capture["mcp_servers"]
        if live
        else [{"type": "url", "name": "private", "url": "https://private.invalid"}]
        if getattr(request, "param", None) == "mcp"
        else (),
    )
    if live:
        native = native.model_copy(update={"execution_identity": capture["execution_identity"]})
    if getattr(request, "param", None) in ("haiku55_effort", "sonnet55_effort"):
        from anthropic.types.beta import BetaManagedAgentsModelConfig

        # sonnet55_effort is the staging #fleet-test root: Sonnet 5.5 at effort high.
        model = (
            {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
            if request.param == "haiku55_effort"
            else {"id": "claude-sonnet-5-5", "speed": "standard", "effort": {"type": "high"}}
        )
        native = native.model_copy(
            update={"model": BetaManagedAgentsModelConfig.model_validate(model)}
        )
    environment = ma_environment(tenant_id=identity.tenant_id)
    if live:
        from anthropic.types.beta import BetaEnvironment

        environment = BetaEnvironment.model_validate(
            {
                **environment.model_dump(mode="json"),
                **capture["environment"],
            }
        )
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "GET",
            "/v1/agents/root-source",
            httpx.Response(200, json=native.model_dump(mode="json", exclude_unset=True)),
        )
    )
    async with trace.client() as client:
        actual = await retrieve_agent(client, native.id, scope=scope)
        source = await load_backend_agent(
            client,
            sessions,
            tenant_id=identity.tenant_id,
            agent_name="helper",
            actual_agent=actual,
        )
        async with sessions.begin() as db:
            await agent_memory_stores.insert_memory_store(
                db,
                tenant_id=identity.tenant_id,
                agent_id=source.agent_id,
                memory_store_id="memstore-captured",
            )
        from daimon.core.config import McpSettings
        from pydantic import SecretStr

        source = await project_session_resources(
            source,
            agent=actual,
            environment=environment,
            session_factory=sessions,
            tenant_id=identity.tenant_id,
            agent_uuid=source.agent_id,
            account_id=actor.account_id,
            resource_scope=scope,
            memory_read_only=True,
            mcp_settings=McpSettings(
                public_url="https://mcp.invalid/mcp", jwt_secret=SecretStr("offline-placeholder")
            )
            if getattr(request, "param", None)
            in ("caller_credentials", "haiku55_credentials", "haiku55_effort", "sonnet55_effort")
            else None,
        )
    trace.assert_consumed()
    assert [(r.method, r.path, r.body) for r in trace.requests] == [
        ("GET", "/v1/agents/root-source", b""),
    ]
    value = AgentFleetGrant(
        max_workers=4,
        choices=(
            (backend_selection("anthropic"), ModelRef(provider="anthropic", id="claude-haiku-5-5")),
            (backend_selection("openai"), ModelRef(provider="openai", id="gpt-6-luna")),
            (
                backend_selection("gemini").model_copy(update={"model": "gemini-3.5-flash-lite"}),
                ModelRef(provider="gemini", id="gemini-3.5-flash-lite"),
            ),
        ),
        limits=WorkerLimits(
            budget_usd=Decimal("0.9"), deadline=NOW + timedelta(hours=1), max_turns=1
        ),
        max_run_seconds=600,
        max_worker_seconds=120,
        max_root_turns=3,
    )
    preview = await service.preview_fleet_grant(source, value, now=NOW)
    await service.set_fleet_grant(
        source,
        value,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=NOW,
    )
    grant = await service.read_fleet_grant(source.tenant_id, source.agent_id, now=NOW)
    async with sessions() as db:
        policy = await load_access_policy(db, tenant_id=source.tenant_id)
    thread = ThreadRef(
        channel=ChannelRef(tenant_id=scope.tenant_id, platform="discord", channel_id="channel"),
        thread_id="thread",
    )
    place = Place(channel_id="thread", parent_channel_id="channel", category_id="category")
    admission = AdmissionGrant(
        tenant_id=source.tenant_id,
        subject=Subject(platform_user_id="member"),
        agent=build_agent_ref(actual.name, actual.metadata or {}),
        surface=Surface.CHANNEL,
        turn_place=place,
        run_place=place,
        channel_id="channel",
        thread_id="thread",
        is_dm=False,
    )
    fleet = FleetFacts(
        scope=scope,
        root=thread,
        origin_id=uuid4(),
        account_id=actor.account_id,
        run_id=uuid4(),
        place=place,
        enabled=True,
        active=True,
    )
    return source, admission, grant, fleet, policy, trace


def compose(inputs, **changes):
    source, admission, grant, fleet, policy, _ = inputs
    arguments = dict(fleet=fleet, policy=policy, now=NOW, seal_ids=frozenset({"root-seal"}))
    arguments.update(changes)
    return fleet_root_permissions(source, admission, grant, **arguments)


async def test_real_source_and_scoped_admin_grant_preserve_exact_authority(captured):
    source, admission, grant, fleet, _, trace = captured
    permissions = compose(captured)
    assert permissions.fleet == fleet and permissions.subject == admission.subject
    assert permissions.agent == admission.agent and permissions.agent_id == source.agent_id
    assert permissions.backend_grants == grant.backends and permissions.models == grant.models
    assert permissions.spec.system == source.effective_spec.system
    assert permissions.spec.tools[0].name == "helper"
    assert permissions.environment == source.environment.spec
    assert permissions.source_revisions == ((source.source.ref, source.source.revision),)
    assert permissions.resources[0].resource.id == source.memory_store_id
    assert permissions.resources[0].resource.account_id == fleet.scope.account_id
    assert permissions.resources[0].native.value["resource"]["access"] == "read_only"
    assert permissions.seal_ids == frozenset({"root-seal"}) and permissions.memory_read_only
    # The run's time (600 s); each worker's 120 s is applied at spawn.
    assert permissions.limits.deadline == NOW + timedelta(seconds=600)
    assert permissions.max_worker_seconds == 120
    assert len(trace.requests) == 1


@pytest.mark.parametrize("role", (0, 1, 2))
async def test_each_exact_role_pair_derives_without_source_model_substitution(captured, role):
    permissions = compose(captured)
    backend, model = captured[2].choices[role]
    spec = permissions.spec.model_copy(
        update={
            "model": model,
            "tools": (),
            "skills": (),
            "mcp_servers": (),
            "declared_drops": (
                "memory",
                "sandbox",
                "skills",
                "mcp",
                "tools",
                "files",
                "repository",
            ),
        }
    )
    from mux.contracts.config import BackendConfig

    child = resolve_worker_spec(
        permissions,
        spec,
        policy=captured[4],
        now=NOW,
        backend=BackendConfig.model_validate(backend.model_dump()),
        limits=permissions.limits,
        captured_source=captured[0],
    )
    assert child.backend == backend and child.spec.model == model
    assert not child.permissions.subject.is_admin
    assert child.permissions.backend_grants == (backend,) and child.permissions.models == (model,)


@pytest.mark.parametrize("captured", ("mcp",), indirect=True)
async def test_actual_projector_keeps_unsupported_main_credentials_and_mcp(captured):
    source, _, _, _, _, trace = captured
    assert source.resource_requirements or source.effective_spec.mcp_servers
    with pytest.raises(WorkerSpecRefused):
        compose(captured)
    assert len(trace.requests) == 1


@pytest.mark.parametrize("captured", ("caller_credentials",), indirect=True)
async def test_a_caller_login_connector_is_left_out_of_the_worker_ceiling(captured):
    """The connector with the caller's login stays the root's; workers never get it."""
    source, _, _, _, _, trace = captured
    logins = {binding.credential_ref for binding in source.resource_requirements}
    assert logins - {None}
    root = compose(captured)
    assert not {binding.credential_ref for binding in root.resources} & logins
    assert not {server.credential_ref for server in root.spec.mcp_servers or ()} & logins
    assert "mcp" in root.spec.declared_drops
    dumped = json.dumps(
        [root.spec.model_dump(mode="json"), [r.model_dump(mode="json") for r in root.resources]]
    )
    assert not any(str(ref) in dumped for ref in logins - {None})
    assert len(trace.requests) == 1


async def test_finite_grant_durations_remain_under_the_existing_turn_ceiling(captured):
    from daimon.core.turn.ceiling import TURN_CEILING_S

    source, admission, grant, fleet, policy, _ = captured
    grant = grant.model_copy(update={"max_run_seconds": 10**100, "max_worker_seconds": 10**100})
    permissions = fleet_root_permissions(
        source, admission, grant, fleet=fleet, policy=policy, now=NOW
    )
    assert permissions.limits.deadline == NOW + timedelta(seconds=TURN_CEILING_S)


@pytest.mark.parametrize(
    "fault",
    (
        "credential",
        "mcp",
        "opaque",
        "repository",
        "unknown_extension",
        "native_tools",
        "foreign_scope",
        "foreign_environment",
        "archived_environment",
        "expired",
        "pending_epoch",
        "invalid_grant",
        "naive_clock",
        "policy",
        "category",
        "subject",
    ),
)
async def test_unsupported_missing_or_foreign_capture_refuses_before_native_io(captured, fault):
    source, admission, grant, fleet, policy, trace = captured
    if fault in ("credential", "opaque"):
        source = source.model_copy(
            update={
                "resource_requirements": (
                    ResourceBinding(
                        id="retained", kind="credential" if fault == "credential" else "native"
                    ),
                )
            }
        )
    elif fault == "mcp":
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={
                        "mcp_servers": (
                            MCPConnection(name="private", url="https://private.invalid"),
                        ),
                    }
                )
            }
        )
    elif fault == "repository":
        from mux.contracts.resources import WorkspaceSource

        source = source.model_copy(
            update={
                "repository_sources": (
                    WorkspaceSource(
                        kind="repository", target_path=".", repository_url="https://repo.invalid"
                    ),
                )
            }
        )
    elif fault in ("unknown_extension", "native_tools"):
        extension = ExtensionConfig(namespace="anthropic.unrepresented_intent", version=1, value={})
        if fault == "native_tools":
            # A per-tool name that fails the closed builtin-tool schema: still a
            # schema failure, distinct from a well-formed toolset with no neutral
            # form (e.g. a bare `agent_toolset_20260401` or an `mcp_toolset`),
            # which is excluded from the ceiling rather than refused.
            extension = ExtensionConfig(
                namespace="anthropic.agent_tools",
                version=1,
                value={
                    "tools": [
                        {
                            "type": "agent_toolset_20260401",
                            "configs": [{"name": "nonexistent_tool", "enabled": True}],
                        }
                    ],
                },
            )
        source = source.model_copy(
            update={
                "effective_spec": source.effective_spec.model_copy(
                    update={
                        "extensions": source.effective_spec.extensions + (extension,),
                    }
                )
            }
        )
    elif fault == "foreign_scope":
        source = source.model_copy(
            update={"resource_scope": fleet.scope.model_copy(update={"account_id": str(uuid4())})}
        )
    elif fault in ("foreign_environment", "archived_environment"):
        environment = source.environment
        environment = (
            environment.model_copy(
                update={"ref": environment.ref.model_copy(update={"tenant_id": str(uuid4())})}
            )
            if fault == "foreign_environment"
            else environment.model_copy(update={"archived_at": NOW})
        )
        source = source.model_copy(update={"environment": environment})
    elif fault == "expired":
        grant = grant.model_copy(
            update={"limits": grant.limits.model_copy(update={"deadline": NOW})}
        )
    elif fault == "pending_epoch":
        grant = grant.model_copy(update={"epoch": 0})
    elif fault == "invalid_grant":
        grant = grant.model_copy(update={"max_workers": True})
    elif fault == "policy":
        policy = TenantAccessPolicy(channel_rules={"category": ChannelRule(writers="none")})
    elif fault == "category":
        admission = replace(
            admission, run_place=replace(admission.run_place, category_id="neighbour")
        )
    elif fault == "subject":
        admission = replace(admission, tenant_id=uuid4())
    with pytest.raises(WorkerSpecRefused):
        fleet_root_permissions(
            source,
            admission,
            grant,
            fleet=fleet,
            policy=policy,
            now=NOW.replace(tzinfo=None) if fault == "naive_clock" else NOW,
        )
    assert len(trace.requests) == 1


async def test_a_root_before_its_memory_store_exists_runs_workers_without_memory(captured):
    """The store is created by the agent's first session: its workers declare the loss."""
    source, admission, grant, fleet, policy, trace = captured
    source = source.model_copy(update={"memory_store_id": None})
    permissions = fleet_root_permissions(
        source, admission, grant, fleet=fleet, policy=policy, now=NOW
    )
    assert "memory" in permissions.spec.declared_drops
    assert not any(
        r.resource is not None and r.resource.kind == "memory_store" for r in permissions.resources
    )
    assert len(trace.requests) == 1
    # So it is no obstacle either: the memory obstacle copy is for an existing store only.
    assert fleet_root_obstacle(source) is None


_HAIKU55_BUILTINS = ("bash", "read", "edit", "grep", "glob", "write", "web_fetch", "web_search")


@pytest.mark.parametrize("captured", ("haiku55", "haiku55_credentials"), indirect=True)
async def test_fleet_root_ceiling_keeps_builtins_and_excludes_the_daimon_mcp_toolset(captured):
    """Live readback structure, with synthetic skill IDs and no credentials."""
    permissions = compose(captured)
    assert {tool.name for tool in permissions.spec.tools} == set(_HAIKU55_BUILTINS)
    assert all(tool.kind == "builtin" for tool in permissions.spec.tools)
    assert not any(tool.kind == "mcp_toolset" for tool in permissions.spec.tools)
    assert not permissions.spec.mcp_servers
    assert not any(r.kind == "credential" for r in permissions.resources)
    assert len(permissions.spec.skills) == 11
    assert len(captured[5].requests) == 1


@pytest.mark.parametrize("captured", ("haiku55", "haiku55_credentials"), indirect=True)
async def test_worker_cannot_inherit_the_excluded_daimon_mcp_toolset(captured):
    """A worker that asks for the excluded mcp toolset is refused by the
    existing ceiling-subset attenuation — the exclusion in fleet_root_permissions
    is enough; no worker can ever request what never entered the ceiling."""
    permissions = compose(captured)
    requested = permissions.spec.model_copy(
        update={"tools": (ToolSpec(name="daimon-mcp", kind="mcp_toolset"),)}
    )
    with pytest.raises(WorkerSpecRefused) as refused:
        resolve_worker_spec(
            permissions,
            requested,
            policy=captured[4],
            now=NOW,
            limits=permissions.limits,
            captured_source=captured[0],
        )
    assert refused.value.field == "tools" and refused.value.reason == "not_subset"


async def test_fleet_schema_error_refuses_before_freeze(captured, monkeypatch):
    from daimon.core import neutral_resources

    source = captured[0]
    invalid = ExtensionConfig(
        namespace="anthropic.agent_tools", version=1, value={"tools": [{"type": "unknown"}]}
    )
    source = source.model_copy(
        update={
            "effective_spec": source.effective_spec.model_copy(update={"extensions": (invalid,)})
        }
    )

    def freeze(*args, **kwargs):
        pytest.fail("schema-invalid source reached freeze")

    monkeypatch.setattr(neutral_resources, "freeze_root_permissions", freeze)
    with pytest.raises(WorkerSpecRefused, match="tools"):
        fleet_root_permissions(
            source, captured[1], captured[2], fleet=captured[3], policy=captured[4], now=NOW
        )


@pytest.mark.parametrize("captured", ("haiku55", "haiku55_credentials"), indirect=True)
async def test_native_clone_inherits_only_the_projected_ceiling(captured):
    from mux.drivers.anthropic.resources.agents import agent_payload

    permissions = compose(captured)
    child = resolve_worker_spec(
        permissions,
        captured[0].source,
        policy=captured[4],
        now=NOW,
        limits=permissions.limits,
        resources=permissions.resources,
        environment=permissions.environment,
        captured_source=captured[0],
    )
    assert child.spec.tools == permissions.spec.tools
    assert not child.spec.mcp_servers and not child.spec.extensions
    payload = agent_payload(child.spec)
    assert all(t["default_config"]["enabled"] is False for t in payload["tools"])
    assert {c["name"] for t in payload["tools"] for c in t["configs"]} == set(_HAIKU55_BUILTINS)


@pytest.mark.parametrize(
    "captured", ("haiku55", "haiku55_credentials", "haiku55_effort"), indirect=True
)
async def test_live_manifest_worker_session_build_uses_only_neutral_grants(
    captured, owned, monkeypatch
):
    from daimon.core.config import McpSettings
    from daimon.core.mux_compat import sdk_agent
    from daimon.core.sessions import create_session
    from mux.drivers.anthropic.resources.agents import agent_payload
    from mux.testing.drivers.anthropic.ma import MARouter
    from mux.testing.drivers.anthropic.ma_models import ma_session
    from pydantic import SecretStr

    source, _, _, _, policy, _ = captured
    from daimon.core import sessions as session_module

    async def no_root_env(*args, **kwargs):
        # #1357 review: a worker's agent_uuid is its root's; the root's .env never mounts.
        raise AssertionError("a worker session uploaded the root's stored .env")

    monkeypatch.setattr(session_module, "upload_env_and_mount", no_root_env)

    async def root_in_app_mode(*args, **kwargs):
        return True

    async def no_root_vault(*args, **kwargs):
        raise AssertionError("a worker session built the root's GitHub app vault")

    # Even an app-mode root never lends a worker its app vault or mirrored logins.
    monkeypatch.setattr(session_module, "_session_app_mode", root_in_app_mode)
    monkeypatch.setattr(session_module, "effective_app_grant_view", no_root_vault)
    monkeypatch.setattr(session_module, "create_session_vault", no_root_vault)
    permissions = compose(captured)
    worker = resolve_worker_spec(
        permissions,
        permissions.spec,
        policy=policy,
        now=NOW,
        resources=permissions.resources,
        environment=permissions.environment,
        limits=permissions.limits,
        captured_source=source,
    )
    source = source.model_copy(update={"resource_scope": worker.permissions.fleet.scope})
    execution = source.source.model_copy(
        update={
            "ref": source.source.ref.model_copy(update={"id": "worker-agent"}),
            "native": None,
            "spec": worker.spec,
        }
    )
    executing = sdk_agent(execution)
    if worker.spec.extensions:
        expected_model = {
            "id": "claude-haiku-5-5",
            "speed": "standard",
            "effort": {"type": "medium"},
        }
        assert agent_payload(worker.spec)["model"] == expected_model
        assert executing.model.model_dump(mode="json", exclude_unset=True) == expected_model
    from anthropic.types.beta import BetaEnvironment

    environment = BetaEnvironment.model_validate(dict(source.environment.native))
    router = MARouter()
    snapshots = {}

    def native(call, match):
        if call.method == "POST":
            payload = json.loads(call.content)
            session = ma_session(
                id="worker-session",
                agent=executing,
                environment_id=environment.id,
                metadata=payload["metadata"],
                resources=payload["resources"],
            )
            snapshots["session"] = session.model_dump(mode="json")
        return httpx.Response(200, json=snapshots["session"])

    router.add("POST", r"/v1/sessions", native)
    router.add("GET", r"/v1/sessions/worker-session", native)
    router.add(
        "GET",
        r"/v1/environments/.*",
        lambda call, match: httpx.Response(200, json=dict(source.environment.native)),
    )
    trace = ScriptedTransport(router=router)

    async def fence():
        return None

    settings = (
        McpSettings(
            public_url="https://mcp.invalid/mcp", jwt_secret=SecretStr("offline-placeholder")
        )
        if source.resource_requirements
        else None
    )
    async with trace.client() as client:
        from daimon.core.scope import ResolvedConfig
        from daimon.core.turn.admission import Admission
        from daimon.core.turn.prepare import DEFAULT_MA_CAPABILITIES, ProviderPreparationRequest
        from daimon.core.turn.runtime_manifest import prepare_source
        from daimon.testing.turn_fakes import deps_for_pinned_mux
        from mux.contracts.config import ConfigRevision

        deps = replace(
            deps_for_pinned_mux(client, owned[3]),
            agent_backends=True,
            channel_backends=True,
            mcp=settings or McpSettings(),
        )
        request = ProviderPreparationRequest(
            deps=deps,
            admission=Admission(
                account_id=permissions.fleet.account_id,
                agent=sdk_agent(source.source),
                environment=environment,
                config=ResolvedConfig(agent_name="helper", environment_name="test-env"),
                memory_read_only=True,
                backend_revision=ConfigRevision.create(
                    permissions.fleet.root.channel, 1, worker.backend
                ),
                backend_revision_origin="agent",
            ),
            scope=worker.permissions.fleet.scope,
            tenant_id=source.tenant_id,
            platform="discord",
            external_user_id="member",
            thread_id="worker-thread",
            session_account_id=permissions.fleet.account_id,
            reuse_existing=True,
            capabilities=DEFAULT_MA_CAPABILITIES,
            transfer=None,
            deadline=worker.limits.deadline,
            now=lambda: NOW,
            worker=worker,
            source=source,
        )
        prepared = await prepare_source(request)
        assert prepared.target_spec == worker.spec
        result = await create_session(
            client,
            agent=executing,
            environment=environment,
            tenant_id=source.tenant_id,
            account_id=permissions.fleet.account_id,
            agent_uuid=source.agent_id,
            session_factory=owned[3],
            resource_source=source,
            worker=worker,
            before_create=fence,
            memory_read_only=True,
            mcp_settings=settings,
        )
    assert not result.agent.mcp_servers
    assert len(result.agent.skills) == 11
    assert len(result.resources) == 1
    assert len(executing.tools) == 1
    assert agent_payload(worker.spec)["tools"][0]["default_config"]["enabled"] is False
    assert not any("vault" in call.path for call in trace.requests)
    assert not any("/v1/files" in call.path for call in trace.requests)
    trace.assert_consumed()


@pytest.mark.parametrize("captured", ("haiku55",), indirect=True)
async def test_same_provider_native_loss_is_declared_unless_projecting_a_worker_ceiling(captured):
    from daimon.core.turn.runtime_manifest import classify_source_obligations

    permissions = compose(captured)
    missing = classify_source_obligations(
        captured[0],
        permissions.spec,
        environment=permissions.environment,
        resources=permissions.resources,
    )
    assert {"tools", "mcp"} <= missing
    assert not classify_source_obligations(
        captured[0],
        permissions.spec,
        environment=permissions.environment,
        resources=permissions.resources,
        worker_ceiling=True,
    )


async def test_excluded_toolset_removes_its_credential_ref_only(captured):
    from daimon.core.turn.runtime_manifest import worker_source_ceiling

    source = captured[0]
    native = ExtensionConfig(
        namespace="anthropic.agent_tools",
        version=1,
        value={"tools": [{"type": "mcp_toolset", "mcp_server_name": "private"}]},
    )
    source = source.model_copy(
        update={
            "effective_spec": source.effective_spec.model_copy(
                update={
                    "extensions": (native,),
                    "mcp_servers": (
                        MCPConnection(
                            name="private",
                            url="https://private.invalid",
                            credential_ref="private-key",
                        ),
                    ),
                }
            ),
            "resource_requirements": (
                ResourceBinding(
                    id="native-credential", kind="credential", credential_ref="private-key"
                ),
                ResourceBinding(
                    id="unrelated-credential", kind="credential", credential_ref="other-key"
                ),
            ),
        }
    )
    ceiling = worker_source_ceiling(source)
    assert not ceiling.effective_spec.mcp_servers
    assert ceiling.resource_requirements == (source.resource_requirements[1],)


@pytest.mark.parametrize("captured", ["haiku55", "haiku55_effort"], indirect=True)
async def test_recorded_i7_heterogeneous_grant_prepares_all_four_workers(
    captured, owned, record_property
):
    from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
    from daimon.core.scope import ResolvedConfig
    from daimon.core.turn.admission import Admission
    from daimon.core.turn.prepare import DEFAULT_MA_CAPABILITIES, ProviderPreparationRequest
    from daimon.core.turn.runtime_manifest import prepare_source
    from daimon.testing.turn_fakes import deps_for_pinned_mux
    from mux.contracts.config import ConfigRevision

    plan = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures/i7_r6_planned_fleet_grant.json"
        ).read_text()
    )
    now = datetime.fromisoformat(plan["recorded_now"])
    grant = AgentFleetGrant.model_validate_json(json.dumps(plan["grant"]))
    source, admission, _, fleet, policy, _ = captured
    root = fleet_root_permissions(source, admission, grant, fleet=fleet, policy=policy, now=now)
    assert root.limits.budget_usd == Decimal("0.49")
    assert dict(root.limits.provider_limits_usd) == {
        "anthropic": Decimal("0.04"),
        "openai": Decimal("0.055"),
        "gemini": Decimal("0.49"),
    }
    assert grant.native_root_budget_usd == Decimal("0.02")
    assert grant.root_cash_budget_usd == Decimal("0.06")
    holds = {provider: Decimal(value) for provider, value in plan["provider_holds_usd"].items()}
    assert sum(holds.values()) == Decimal("0.70")
    assert holds["anthropic"] == grant.root_cash_budget_usd + Decimal("0.04")
    assert holds["openai"] == Decimal("0.055") * 2
    trace = ScriptedTransport()
    async with trace.client() as client:
        for row in plan["workers"]:
            backend, model = next(
                pair for pair in grant.choices if pair[1].provider == row["provider"]
            )
            native = model.provider == source.source.spec.model.provider
            spec = (
                root.spec
                if native
                else root.spec.model_copy(
                    update={
                        "model": model,
                        "tools": (),
                        "skills": (),
                        "mcp_servers": (),
                        "declared_drops": (
                            "mcp",
                            "memory",
                            *(("model_controls",) if root.spec.extensions else ()),
                            "sandbox",
                            "skills",
                            "tools",
                        ),
                    }
                )
            )
            limits = WorkerLimits(
                budget_usd=Decimal(row["budget_usd"]), deadline=root.limits.deadline, max_turns=1
            )
            worker = resolve_worker_spec(
                root,
                spec,
                policy=policy,
                now=now,
                backend=backend,
                resources=root.resources if native else (),
                environment=root.environment if native else None,
                limits=limits,
                captured_source=source,
            )
            assert worker.limits.budget_usd == min(
                grant.limits.budget_usd, grant.limits.provider_limits_usd[model.provider]
            )
            assert worker.limits.provider_limits_usd == root.limits.provider_limits_usd
            worker_source = source.model_copy(
                update={"resource_scope": worker.permissions.fleet.scope}
            )
            prepared = await prepare_source(
                ProviderPreparationRequest(
                    deps=replace(
                        deps_for_pinned_mux(client, owned[3]),
                        agent_backends=True,
                        channel_backends=True,
                        turn_path="mux",
                    ),
                    admission=Admission(
                        account_id=fleet.account_id,
                        agent=BetaManagedAgentsAgent.model_validate(source.source.native),
                        environment=BetaEnvironment.model_validate(source.environment.native),
                        config=ResolvedConfig(agent_name="helper", environment_name="default"),
                        grant=admission,
                        backend_revision=ConfigRevision.create(
                            fleet.root.channel, 1, worker.backend
                        ),
                        backend_revision_origin="agent",
                    ),
                    scope=worker.permissions.fleet.scope,
                    tenant_id=source.tenant_id,
                    platform="discord",
                    external_user_id="member",
                    thread_id=fleet.root.thread_id,
                    session_account_id=fleet.account_id,
                    reuse_existing=True,
                    capabilities=DEFAULT_MA_CAPABILITIES,
                    transfer=None,
                    deadline=worker.limits.deadline,
                    now=lambda: now,
                    source=worker_source,
                    worker=worker,
                )
            )
            assert prepared.target_spec == worker.spec, row["role"]
            record_property(
                row["role"],
                json.dumps(
                    {
                        "provider": model.provider,
                        "budget_usd": str(worker.limits.budget_usd),
                        "preparation": "PASS",
                    }
                ),
            )
    assert not trace.requests


@pytest.mark.parametrize("captured", ["haiku55_effort"], indirect=True)
async def test_recorded_effort_root_freezes_and_derives_exact_native_or_declared_foreign_controls(
    captured,
):
    from daimon.core.neutral_resources import freeze_root_permissions
    from mux.contracts.config import BackendConfig
    from mux.drivers.anthropic.resources.agents import agent_payload

    source, admission, _, _, policy, _ = captured
    parent = compose(captured)
    frozen = freeze_root_permissions(parent, source, admission=admission, policy=policy, now=NOW)
    expected = {"id": "claude-haiku-5-5", "speed": "standard", "effort": {"type": "medium"}}
    for inherited in (
        frozen.spec,
        frozen.spec.model_copy(update={"extensions": None}),
        source.source,
    ):
        child = resolve_worker_spec(
            frozen,
            inherited,
            policy=policy,
            now=NOW,
            captured_source=source,
            resources=frozen.resources,
            environment=frozen.environment,
            limits=frozen.limits,
        )
        assert agent_payload(child.spec)["model"] == expected
        assert child.spec.extensions == frozen.spec.extensions
        assert not child.spec.mcp_servers
        assert not child.permissions.subject.is_admin
    for backend, model in captured[2].choices[1:]:
        drops = ("mcp", "memory", "model_controls", "sandbox", "skills", "tools")
        spec = frozen.spec.model_copy(
            update={
                "model": model,
                "tools": (),
                "skills": (),
                "mcp_servers": (),
                "declared_drops": drops,
            }
        )
        kwargs = dict(
            policy=policy,
            now=NOW,
            captured_source=source,
            backend=BackendConfig.model_validate(backend.model_dump()),
            limits=frozen.limits,
        )
        with pytest.raises(WorkerSpecRefused, match="extensions"):
            resolve_worker_spec(
                frozen,
                spec.model_copy(
                    update={"declared_drops": tuple(x for x in drops if x != "model_controls")}
                ),
                **kwargs,
            )
        child = resolve_worker_spec(frozen, spec, **kwargs)
        assert child.spec.extensions is None
        assert child.spec.declared_drops == drops
        assert child.permissions.models == (model,)
    for update in (
        {"value": {"model": {**expected, "effort": "max"}}},
        {"value": {"model": {**expected, "unknown_control": True}}},
        {"value": {"model": {**expected, "id": "another-model"}}},
        {"version": 2},
        {"namespace": "unknown.authority"},
    ):
        changed = frozen.spec.model_copy(
            update={
                "extensions": tuple(
                    extension.model_copy(update=update) for extension in frozen.spec.extensions
                )
            }
        )
        with pytest.raises(WorkerSpecRefused, match=r"(?:spec|extensions): unsupported"):
            resolve_worker_spec(frozen, changed, policy=policy, now=NOW)


def _run(*, total: str = "2.00", max_workers: int = 4):
    from types import SimpleNamespace

    return SimpleNamespace(total_budget_usd=Decimal(total), max_workers=max_workers)


def _without(grant, provider):
    return grant.model_copy(
        update={"choices": tuple(pair for pair in grant.choices if pair[1].provider != provider)}
    )


async def test_fleet_r1_root_model_missing_from_grant_is_granted_implicitly(captured):
    """r1: a grant without the root's own model refused every turn of the root."""
    source, _, grant, _, policy, _ = captured
    root = ModelRef(provider="anthropic", id="claude-haiku-5-5")
    no_root_provider = _without(grant, "anthropic")
    permissions = compose(captured[:2] + (no_root_provider,) + captured[3:])
    assert permissions.models == (*no_root_provider.models, root)
    assert permissions.backend_grants == (
        *no_root_provider.backends,
        backend_selection("anthropic"),
    )
    sonnet = no_root_provider.model_copy(
        update={
            "choices": (
                (
                    backend_selection("anthropic"),
                    ModelRef(provider="anthropic", id="claude-sonnet-5-5"),
                ),
                *no_root_provider.choices,
            )
        }
    )
    permissions = compose(captured[:2] + (sonnet,) + captured[3:])
    assert permissions.models == (*sonnet.models, root)
    assert permissions.backend_grants == sonnet.backends
    worker = resolve_worker_spec(
        permissions,
        permissions.spec.model_copy(update={"tools": (), "declared_drops": ("tools",)}),
        policy=policy,
        now=NOW,
        captured_source=source,
        resources=permissions.resources,
        environment=permissions.environment,
    )
    assert worker.spec.model.id == root.id and worker.backend.backend == "anthropic"


async def test_fleet_r1_root_model_is_not_added_for_a_disabled_provider(captured):
    from daimon.core.neutral_resources import (
        _with_root_model,  # pyright: ignore[reportPrivateUsage]
    )

    _, _, grant, fleet, policy, _ = captured
    reduced = _without(grant, "anthropic")
    disabled = policy.model_copy(update={"enabled_backends": ("openai", "gemini")})
    root = ModelRef(provider="anthropic", id="claude-haiku-5-5")
    assert _with_root_model(reduced, root, policy=disabled, fleet=fleet) == (
        reduced.backends,
        reduced.models,
    )
    assert _with_root_model(grant, root, policy=policy, fleet=fleet) == (
        grant.backends,
        grant.models,
    )


@pytest.mark.parametrize("captured", ["sonnet55_effort"], indirect=True)
async def test_fleet_r2_sonnet_effort_root_spawns_a_haiku_worker_without_its_effort(
    captured, owned
):
    """r2: a Haiku worker under the Sonnet effort=high root was refused outright.

    Haiku may not accept the root's effort level, so the worker declares the loss
    and runs at its model's defaults; an undeclared mismatch refuses before admission.
    """
    from daimon.core.neutral_resources import task_worker_spec
    from mux.drivers.anthropic.resources.agents import agent_payload

    source, _, grant, _, policy, _ = captured
    haiku = ModelRef(provider="anthropic", id="claude-haiku-5-5")
    root = compose(captured)
    # r1 too: the grant lists Haiku, not the Sonnet root, which is now implicit.
    assert root.models[-1] == ModelRef(provider="anthropic", id="claude-sonnet-5-5")
    kwargs = dict(
        policy=policy,
        now=NOW,
        captured_source=source,
        resources=root.resources,
        environment=root.environment,
    )
    spec = task_worker_spec(root, role="W1", task="Compare X and Y.", model=haiku.id)
    assert "model_controls" in spec.declared_drops
    child = resolve_worker_spec(root, spec, **kwargs)
    assert child.spec.extensions is None
    assert agent_payload(child.spec)["model"] == "claude-haiku-5-5"
    assert child.permissions.models == (haiku,)
    for undeclared in (
        spec.model_copy(update={"declared_drops": ("skills", "tools")}),
        spec.model_copy(
            update={"declared_drops": ("skills", "tools"), "extensions": root.spec.extensions}
        ),
    ):
        with pytest.raises(WorkerSpecRefused, match="extensions: unsupported"):
            resolve_worker_spec(root, undeclared, **kwargs)
    # The root's own model keeps its effort exactly.
    own = resolve_worker_spec(
        root, task_worker_spec(root, role="W2", task="Compare X and Y."), **kwargs
    )
    assert agent_payload(own.spec)["model"] == {
        "id": "claude-sonnet-5-5",
        "speed": "standard",
        "effort": {"type": "high"},
    }
    assert "model_controls" not in own.declared_drops
    await _prepare_worker(captured, owned, child)
    await _prepare_worker(captured, owned, own)


async def _prepare_worker(captured, owned, worker):
    from anthropic.types.beta import BetaEnvironment, BetaManagedAgentsAgent
    from daimon.core.scope import ResolvedConfig
    from daimon.core.turn.admission import Admission
    from daimon.core.turn.prepare import DEFAULT_MA_CAPABILITIES, ProviderPreparationRequest
    from daimon.core.turn.runtime_manifest import prepare_source
    from daimon.testing.turn_fakes import deps_for
    from mux.contracts.config import ConfigRevision

    source, admission, _, fleet, _, _ = captured
    trace = ScriptedTransport()
    async with trace.client() as client:
        prepared = await prepare_source(
            ProviderPreparationRequest(
                deps=replace(
                    deps_for(client, owned[3]),
                    agent_backends=True,
                    channel_backends=True,
                    turn_path="mux",
                ),
                admission=Admission(
                    account_id=fleet.account_id,
                    agent=BetaManagedAgentsAgent.model_validate(source.source.native),
                    environment=BetaEnvironment.model_validate(source.environment.native),
                    config=ResolvedConfig(agent_name="helper", environment_name="default"),
                    grant=admission,
                    backend_revision=ConfigRevision.create(fleet.root.channel, 1, worker.backend),
                    backend_revision_origin="agent",
                ),
                scope=worker.permissions.fleet.scope,
                tenant_id=source.tenant_id,
                platform="discord",
                external_user_id="member",
                thread_id=fleet.root.thread_id,
                session_account_id=fleet.account_id,
                reuse_existing=True,
                capabilities=DEFAULT_MA_CAPABILITIES,
                transfer=None,
                deadline=worker.limits.deadline,
                now=lambda: NOW,
                source=source.model_copy(update={"resource_scope": worker.permissions.fleet.scope}),
                worker=worker,
            )
        )
    assert prepared.target_spec == worker.spec
    assert not trace.requests


@pytest.mark.parametrize(
    "captured", ["haiku55", "haiku55_effort", "sonnet55_effort"], indirect=True
)
async def test_fleet_r3_r4_plain_task_workers_resolve_with_default_limits(captured):
    """r3/r4: a plain ask needs no run_id, backend, budget or deadline arithmetic."""
    from daimon.core.neutral_resources import default_worker_limits, task_worker_spec

    source, _, grant, _, policy, _ = captured
    root = compose(captured)
    for model in (None, *(model.id for model in grant.models)):
        spec = task_worker_spec(root, role="W", task="Compare X and Y.", model=model)
        limits = default_worker_limits(
            root.limits, _run(total="0.90", max_workers=3), (), provider=spec.model.provider
        )
        assert limits.deadline == root.limits.deadline and limits.max_turns == 1
        assert limits.budget_usd == Decimal("0.30")
        child = resolve_worker_spec(
            root,
            spec,
            policy=policy,
            now=NOW,
            captured_source=source,
            resources=root.resources if spec.model.provider == "anthropic" else (),
            environment=root.environment if spec.model.provider == "anthropic" else None,
            limits=limits,
        )
        assert child.spec.model.id == (model or root.spec.model.id)
        assert ("model_controls" in child.declared_drops) == (
            spec.model.id != root.spec.model.id and bool(root.spec.extensions)
        )
    with pytest.raises(WorkerSpecRefused, match="backend: not_subset"):
        task_worker_spec(root, role="W", task="Compare.", model="gemini-3.8-pro")
    with pytest.raises(WorkerSpecRefused, match="system: unbound"):
        task_worker_spec(root, role="W", task="  ")


def _worker(backend: str, budget: str, *, status: str = "pending", reserved: str = "0"):
    from types import SimpleNamespace

    return SimpleNamespace(
        backend=backend,
        budget_usd=Decimal(budget),
        reserved_usd=Decimal(reserved),
        status=status,
        binding_id=None,
        attempts=0,
        turns_used=0,
        result=None,
    )


CEILING = WorkerLimits(
    budget_usd=Decimal("2.00"),
    deadline=NOW + timedelta(minutes=10),
    max_turns=2,
    provider_limits_usd={
        "anthropic": Decimal("0.60"),
        "openai": Decimal("0.60"),
        "gemini": Decimal("0.80"),
    },
)


def test_default_worker_limits_split_per_slot_and_clamp_the_deadline():
    from daimon.core.neutral_resources import default_worker_limits

    run = _run()
    assert default_worker_limits(CEILING, run, (), provider="openai").budget_usd == Decimal("0.15")
    assert default_worker_limits(CEILING, run, (), provider="gemini").budget_usd == Decimal("0.20")
    spent = (_worker("anthropic", "0.30"),)
    assert default_worker_limits(CEILING, run, spent, provider="anthropic").budget_usd == Decimal(
        "0.10"
    )
    assert default_worker_limits(CEILING, run, spent, provider="gemini").budget_usd == Decimal(
        "0.26"
    )
    later = default_worker_limits(
        CEILING, run, (), provider="openai", deadline=NOW + timedelta(hours=1)
    )
    assert later.deadline == CEILING.deadline
    explicit = default_worker_limits(
        CEILING,
        None,
        (),
        provider="openai",
        budget_usd=Decimal("0.10"),
        deadline=NOW + timedelta(seconds=100),
        max_turns=2,
    )
    assert (explicit.budget_usd, explicit.deadline, explicit.max_turns) == (
        Decimal("0.10"),
        NOW + timedelta(seconds=100),
        2,
    )
    with pytest.raises(WorkerSpecRefused, match="budget: unbound"):
        default_worker_limits(CEILING, None, (), provider="openai")


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_parallel_default_spawns_never_exceed_the_run_or_provider_caps(provider):
    """Every worker that can still be admitted read the same stale snapshot."""
    from daimon.core.neutral_resources import default_worker_limits

    run = _run()
    for admitted in range(run.max_workers):
        workers = tuple(_worker(provider, "0.01") for _ in range(admitted))
        racing = run.max_workers - admitted
        share = default_worker_limits(CEILING, run, workers, provider=provider).budget_usd
        line = CEILING.provider_limits_usd[provider] - Decimal("0.01") * admitted
        assert share * racing <= line
        assert share * racing <= run.total_budget_usd - Decimal("0.01") * admitted


def test_exhausted_budget_or_slots_refuse_before_admission():
    from daimon.core.neutral_resources import default_worker_limits

    run = _run()
    with pytest.raises(WorkerSpecRefused, match="budget: exhausted"):
        default_worker_limits(CEILING, run, (_worker("openai", "0.60"),), provider="openai")
    with pytest.raises(WorkerSpecRefused, match="budget: exhausted"):
        default_worker_limits(
            CEILING, _run(max_workers=1), (_worker("gemini", "0.10"),), provider="openai"
        )
    with pytest.raises(WorkerSpecRefused, match="budget: exhausted"):
        default_worker_limits(CEILING, _run(total="0.03"), (), provider="openai")


def test_planned_workers_share_the_budget_instead_of_unused_slots():
    from daimon.core.neutral_resources import default_worker_limits

    unlimited = CEILING.model_copy(update={"provider_limits_usd": None, "budget_usd": Decimal(1)})
    run = _run(total="1.00", max_workers=8)
    assert default_worker_limits(unlimited, run, (), provider="openai").budget_usd == Decimal(
        "0.12"
    )
    assert default_worker_limits(
        unlimited, run, (), provider="openai", planned_workers=2
    ).budget_usd == Decimal("0.50")
    # Planned workers already admitted leave the rest to the remaining ones.
    one = (_worker("openai", "0.50"),)
    assert default_worker_limits(
        unlimited, run, one, provider="openai", planned_workers=2
    ).budget_usd == Decimal("0.50")
    # More than planned, or more than the slots: never more than what is left.
    two = (*one, _worker("openai", "0.50"))
    with pytest.raises(WorkerSpecRefused, match="budget: exhausted"):
        default_worker_limits(unlimited, run, two, provider="openai", planned_workers=2)
    assert default_worker_limits(
        unlimited, _run(total="1.00", max_workers=2), (), provider="openai", planned_workers=8
    ).budget_usd == Decimal("0.50")


@pytest.mark.parametrize("planned", [1, 2, 3, 4])
def test_parallel_planned_spawns_never_exceed_the_caps(planned):
    from daimon.core.neutral_resources import default_worker_limits

    run = _run()
    for provider, line in CEILING.provider_limits_usd.items():
        share = default_worker_limits(
            CEILING, run, (), provider=provider, planned_workers=planned
        ).budget_usd
        assert share * planned <= min(line, run.total_budget_usd)


def test_spawns_refused_at_admission_free_their_slot_and_budget():
    from daimon.core.neutral_resources import default_worker_limits

    unlimited = CEILING.model_copy(update={"provider_limits_usd": None, "budget_usd": Decimal(1)})
    run = _run(total="1.00", max_workers=2)
    admitted = _worker("openai", "0.50", reserved="0.50")
    refused = _worker("openai", "0.50", status="cancelled")
    # Two refused spawns neither fill the 2 slots nor hold the run's money.
    assert default_worker_limits(
        unlimited, run, (admitted, refused, refused), provider="openai", planned_workers=2
    ).budget_usd == Decimal("0.50")


async def test_fleet_r7_only_gemini_workers_default_to_its_runtime_bound(captured):
    """r7: the run's deadline is the default except where a profile caps runtime."""
    from daimon.core.neutral_resources import default_worker_limits, worker_runtime_ceiling

    # No grant worker time: only Gemini's profile caps a worker (10 min).
    root = replace(compose(captured), max_worker_seconds=None)
    assert worker_runtime_ceiling(root, "anthropic") is None
    assert worker_runtime_ceiling(root, "openai") is None
    assert worker_runtime_ceiling(root, "gemini") == 600
    ceiling = root.limits.model_copy(update={"deadline": NOW + timedelta(minutes=20)})
    for provider in ("anthropic", "openai", "gemini"):
        bound = worker_runtime_ceiling(root, provider)
        limits = default_worker_limits(
            ceiling, _run(), (), provider=provider, now=NOW, max_runtime_s=bound
        )
        expected = NOW + timedelta(minutes=10) if provider == "gemini" else ceiling.deadline
        assert limits.deadline == expected
        late = default_worker_limits(
            ceiling,
            _run(),
            (),
            provider=provider,
            deadline=NOW + timedelta(hours=1),
            now=NOW,
            max_runtime_s=bound,
        )
        assert late.deadline == expected
    # The run's own deadline still wins when it is sooner.
    soon = ceiling.model_copy(update={"deadline": NOW + timedelta(seconds=60)})
    assert default_worker_limits(
        soon, _run(), (), provider="gemini", now=NOW, max_runtime_s=600
    ).deadline == NOW + timedelta(seconds=60)
    with pytest.raises(WorkerSpecRefused, match="deadline: unbound"):
        default_worker_limits(ceiling, _run(), (), provider="gemini", max_runtime_s=600)


async def test_the_grants_worker_time_caps_every_worker_and_the_run_keeps_its_own(captured):
    """ROOT 10-min decision: the run gets max_run_seconds; each worker max_worker_seconds."""
    from daimon.core.neutral_resources import worker_runtime_ceiling

    root = compose(captured)
    # The test grant: 600 s a run, 120 s a worker. The root (run) deadline is the run's.
    assert root.max_worker_seconds == 120
    assert root.limits.deadline == NOW + timedelta(seconds=600)
    for provider in ("anthropic", "openai", "gemini"):
        assert worker_runtime_ceiling(root, provider) == 120
    # A grant worker time above Gemini's profile cap leaves Gemini at its cap.
    assert worker_runtime_ceiling(replace(root, max_worker_seconds=900), "gemini") == 600
    assert worker_runtime_ceiling(replace(root, max_worker_seconds=900), "openai") == 900


def test_bounded_gemini_tokens_fit_the_prompt_and_the_budget():
    from daimon.core.neutral_resources import (
        bounded_gemini_worker_tokens,
        gemini_worker_cost_usd,
    )

    system = "Real root instructions. " * 1000  # ~24k characters, as staging's root
    for budget, markup in ((Decimal("0.66"), Decimal(1)), (Decimal("0.15"), Decimal("1.3"))):
        tokens = bounded_gemini_worker_tokens(
            system, budget, model="gemini-3.8-flash", markup=markup
        )
        assert tokens["max_input_tokens"] == len(system.encode()) + 4096
        assert 1024 <= tokens["max_output_tokens"] <= 65_536
        worst = gemini_worker_cost_usd(
            "gemini-3.8-flash", tokens["max_input_tokens"], tokens["max_output_tokens"]
        )
        assert worst <= budget / markup and worst <= Decimal("0.49")
    # A budget that cannot buy a useful answer after the prompt refuses before dispatch.
    with pytest.raises(WorkerSpecRefused, match="budget: exhausted"):
        bounded_gemini_worker_tokens(system, Decimal("0.02"), model="gemini-3.8-flash")


def _in_argon_window(monkeypatch):
    from daimon.core import pricing

    class InArgonWindow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 30, tzinfo=UTC)

    monkeypatch.setattr(pricing, "datetime", InArgonWindow)


def test_bounded_argon_tokens_fit_the_prompt_the_budget_and_the_hold(monkeypatch):
    from daimon.core.neutral_resources import (
        bounded_gemini_worker_tokens,
        gemini_worker_cost_usd,
    )

    _in_argon_window(monkeypatch)
    system = "Real root instructions. " * 1000
    for budget, markup, hold in (
        (Decimal("2.00"), Decimal(1), Decimal("0.49")),
        (Decimal("0.40"), Decimal("1.3"), Decimal("0.49")),
        (Decimal("2.00"), Decimal(1), None),
    ):
        tokens = bounded_gemini_worker_tokens(
            system, budget, model="gemini-4-argon", markup=markup, hold_usd=hold
        )
        assert tokens["max_input_tokens"] == len(system.encode()) + 4096
        worst = gemini_worker_cost_usd(
            "gemini-4-argon", tokens["max_input_tokens"], tokens["max_output_tokens"]
        )
        limit = budget / markup if hold is None else min(budget / markup, hold)
        assert worst <= limit
    # Argon's price is far above Flash's: a Flash-sized budget refuses before dispatch.
    with pytest.raises(WorkerSpecRefused, match="budget: exhausted"):
        bounded_gemini_worker_tokens(system, Decimal("0.05"), model="gemini-4-argon")


@pytest.mark.parametrize("captured", ["haiku55_effort"], indirect=True)
async def test_plain_argon_spawn_fills_the_bounded_envelope_or_refuses(captured, monkeypatch):
    from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused
    from daimon.core.neutral_resources import gemini_worker_cost_usd, task_worker_spec

    _in_argon_window(monkeypatch)
    source, _, grant, _, policy, _ = captured
    argon = ModelRef(provider="gemini", id="gemini-4-argon")
    widened = grant.model_copy(
        update={
            "choices": (
                *grant.choices,
                (backend_selection("gemini").model_copy(update={"model": argon.id}), argon),
            )
        }
    )
    root = compose(captured[:2] + (widened,) + captured[3:])
    owner = FleetOrchestrator()
    spec = task_worker_spec(root, role="W3", task="Compare X and Y.", model=argon.id)

    def resolve(limits):
        return resolve_worker_spec(root, spec, policy=policy, now=NOW, limits=limits)

    base = resolve(root.limits.model_copy(update={"max_turns": 1}))
    filled = owner._bounded_gemini(root, base, resolve, fill=True, trigger="Go.")  # pyright: ignore[reportPrivateUsage]
    limits = filled.limits
    assert limits.max_input_tokens == len(filled.spec.system.encode()) + len("Go.") + 4096
    worst = gemini_worker_cost_usd(argon.id, limits.max_input_tokens, limits.max_output_tokens)
    assert worst <= limits.budget_usd
    # Argon never takes the unbounded 4096-token path: without a fill it refuses.
    with pytest.raises(FleetRefused, match="argon_requires_bounded_worker"):
        owner._bounded_gemini(root, base, resolve, fill=False, trigger="Go.")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("captured", ["haiku55_effort"], indirect=True)
@pytest.mark.parametrize(
    ("model", "fits", "oversized"),
    (
        # Flash: 262144 * $0.75/M + 65536 * $3.75/M = $0.44 worst case.
        ("gemini-3.8-flash", (16384, 8192), (262144, 65536)),
        # Argon: 262144 * $2/M + 65536 * $10/M = $1.18 worst case.
        ("gemini-4-argon", (10_000, 2048), (262144, 65536)),
    ),
)
async def test_explicit_gemini_worker_caps_must_fit_the_worker_budget(
    captured, monkeypatch, model, fits, oversized
):
    from daimon.core.fleet_orchestration import FleetOrchestrator
    from daimon.core.neutral_resources import gemini_worker_cost_usd, task_worker_spec

    _in_argon_window(monkeypatch)
    source, _, grant, _, policy, _ = captured
    ref = ModelRef(provider="gemini", id=model)
    widened = grant.model_copy(
        update={
            "choices": (
                *grant.choices,
                (backend_selection("gemini").model_copy(update={"model": ref.id}), ref),
            )
        }
    )
    root = compose(captured[:2] + (widened,) + captured[3:])
    owner = FleetOrchestrator()
    spec = task_worker_spec(root, role="W3", task="Compare X and Y.", model=ref.id)

    def resolve(limits):
        return resolve_worker_spec(root, spec, policy=policy, now=NOW, limits=limits)

    def explicit(caps):
        update = {
            "max_turns": 1,
            "budget_usd": Decimal("0.05"),
            "max_input_tokens": caps[0],
            "max_output_tokens": caps[1],
        }
        return resolve(root.limits.model_copy(update=update))

    kept = explicit(fits)
    assert owner._bounded_gemini(root, kept, resolve, fill=False, trigger="Go.") is kept  # pyright: ignore[reportPrivateUsage]
    assert gemini_worker_cost_usd(model, *fits) <= Decimal("0.05")
    # Caller-supplied caps whose worst case at the model's tariff exceeds the budget.
    assert gemini_worker_cost_usd(model, *oversized) > Decimal("0.05")
    with pytest.raises(WorkerSpecRefused, match="budget: exhausted"):
        owner._bounded_gemini(root, explicit(oversized), resolve, fill=False, trigger="Go.")  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("captured", ["haiku55_effort"], indirect=True)
async def test_fleet_r9_plain_gemini_spawn_fills_limits_only_when_none_are_given(captured):
    from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused
    from daimon.core.neutral_resources import task_worker_spec

    source, _, grant, _, policy, _ = captured
    flash = ModelRef(provider="gemini", id="gemini-3.8-flash")
    widened = grant.model_copy(
        update={
            "choices": (
                *grant.choices,
                (backend_selection("gemini").model_copy(update={"model": flash.id}), flash),
            )
        }
    )
    root = compose(captured[:2] + (widened,) + captured[3:])
    large = "Real root instructions. " * 1000
    root = replace(root, spec=root.spec.model_copy(update={"system": large}), base_system=large)
    owner = FleetOrchestrator()

    def resolver(spec):
        def resolve(limits):
            return resolve_worker_spec(root, spec, policy=policy, now=NOW, limits=limits)

        return resolve

    plain = task_worker_spec(root, role="W3", task="Compare X and Y.", model=flash.id)
    resolve = resolver(plain)
    base = resolve(root.limits.model_copy(update={"max_turns": 1}))
    filled = owner._bounded_gemini(root, base, resolve, fill=True, trigger="Go.")  # pyright: ignore[reportPrivateUsage]
    # The instructions plus the trigger dispatch sends, plus headroom.
    assert filled.limits.max_input_tokens == len(filled.spec.system.encode()) + len("Go.") + 4096
    assert filled.limits.max_output_tokens is not None
    # Explicit limits from the caller win and are not replaced.
    explicit = resolve(
        root.limits.model_copy(
            update={"max_turns": 1, "max_input_tokens": 262144, "max_output_tokens": 65536}
        )
    )
    kept = owner._bounded_gemini(root, explicit, resolve, fill=True, trigger="Go.")  # pyright: ignore[reportPrivateUsage]
    assert (kept.limits.max_input_tokens, kept.limits.max_output_tokens) == (262144, 65536)
    # Without a fill, a prompt the unbounded 4096-token cap cannot hold is refused clearly.
    with pytest.raises(FleetRefused, match="prompt_exceeds_worker_token_cap"):
        owner._bounded_gemini(root, base, resolve, fill=False, trigger="Go.")  # pyright: ignore[reportPrivateUsage]
    # Other providers are untouched.
    luna = resolver(task_worker_spec(root, role="W2", task="Compare.", model="gpt-6-luna"))
    other = luna(root.limits.model_copy(update={"max_turns": 1}))
    assert owner._bounded_gemini(root, other, luna, fill=True, trigger="Go.") is other  # pyright: ignore[reportPrivateUsage]


def test_a_provider_line_is_shared_only_by_that_providers_planned_workers():
    """Fleet r6: one worker per provider could use only a third of each provider line."""
    from daimon.core.neutral_resources import default_worker_limits

    skewed = CEILING.model_copy(
        update={
            "provider_limits_usd": {
                "anthropic": Decimal("0.30"),
                "openai": Decimal("0.15"),
                "gemini": Decimal("1.55"),
            }
        }
    )
    run = _run()

    def share(provider, on_provider, workers=()):
        return default_worker_limits(
            skewed,
            run,
            workers,
            provider=provider,
            planned_workers=3,
            planned_on_provider=on_provider,
        ).budget_usd

    assert (share("anthropic", 1), share("openai", 1), share("gemini", 1)) == (
        Decimal("0.30"),
        Decimal("0.15"),
        Decimal("0.66"),
    )
    # Without the per-provider count each line is still split three ways.
    assert default_worker_limits(
        skewed, run, (), provider="gemini", planned_workers=3
    ).budget_usd == Decimal("0.51")
    # Two planned on one provider share its line; one already admitted leaves its rest.
    assert share("anthropic", 2) == Decimal("0.15")
    assert share("anthropic", 2, (_worker("anthropic", "0.15"),)) == Decimal("0.15")


@pytest.mark.parametrize(
    "plan",
    [
        ("anthropic", "openai", "gemini"),
        ("gemini", "gemini", "openai"),
        ("anthropic", "anthropic", "anthropic", "openai"),
    ],
)
def test_parallel_spawns_with_a_provider_plan_never_exceed_any_cap(plan):
    from daimon.core.neutral_resources import default_worker_limits

    run = _run()
    spent = {provider: Decimal(0) for provider in CEILING.provider_limits_usd}
    total = Decimal(0)
    for provider in plan:
        amount = default_worker_limits(
            CEILING,
            run,
            (),
            provider=provider,
            planned_workers=len(plan),
            planned_on_provider=plan.count(provider),
        ).budget_usd
        spent[provider] += amount
        total += amount
    assert total <= run.total_budget_usd
    assert all(spent[p] <= CEILING.provider_limits_usd[p] for p in spent)


def test_bounded_gemini_tokens_count_the_trigger_and_respect_the_hold():
    from daimon.core.neutral_resources import (
        bounded_gemini_worker_tokens,
        gemini_worker_cost_usd,
    )

    system = "Real root instructions. " * 1000
    review = json.dumps({"result": {"text": "x" * 50_000}})
    plain = bounded_gemini_worker_tokens(system, Decimal("1"), model="gemini-3.8-flash")
    reviewer = bounded_gemini_worker_tokens(
        system, Decimal("1"), model="gemini-3.8-flash", extra_input=review
    )
    # A reviewer's embedded reviewed result is part of what it sends.
    assert reviewer["max_input_tokens"] == plain["max_input_tokens"] + len(review.encode())
    held = bounded_gemini_worker_tokens(
        system, Decimal("1"), model="gemini-3.8-flash", hold_usd=Decimal("0.10")
    )
    worst = gemini_worker_cost_usd(
        "gemini-3.8-flash", held["max_input_tokens"], held["max_output_tokens"]
    )
    assert (
        worst
        <= Decimal("0.10")
        < gemini_worker_cost_usd(
            "gemini-3.8-flash", plain["max_input_tokens"], plain["max_output_tokens"]
        )
    )


def test_manifest_hold_is_the_row_runtime_selection_would_pick(tmp_path, monkeypatch):
    """Since D3 a tenant-wide "*" Gemini row sits next to #fleet-test's own rows."""
    from daimon.core.turn.runtime_manifest import manifest_hold_usd
    from mux.contracts.config import ConfigRevision

    backend = backend_selection("gemini").model_copy(update={"model": "gemini-3.8-flash"})
    grant = {
        "profiles": [backend.profile],
        "models": ["gemini-3.8-flash"],
        "thread_modes": [backend.thread_mode],
        "requires": {},
    }

    def revision(channel_id):
        channel = ChannelRef(tenant_id="tenant", platform="discord", channel_id=channel_id)
        return ConfigRevision.create(channel, 1, backend)

    def row(**changes):
        base = {
            "profile": backend.profile,
            "tenant_id": "tenant",
            "platform": "discord",
            "channel_id": "fleet-test",
            "account_id": "*",
            "configuration_grant": grant,
            "hold_usd": "0.49",
            # Fleet workers' turns are agent-origin: only source-pinned worker rows serve them.
            "source_agent_id": "00000000-0000-0000-0000-0000000000f1",
            "source_agent_version": 1,
            "worker_materialization": True,
        }
        return base | changes

    unsourced = {"source_agent_id": None, "source_agent_version": None}
    channel_only = {**unsourced, "worker_materialization": False}

    path = tmp_path / "runtime.json"
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))

    def hold(channel_id, account_id="acct", source="00000000-0000-0000-0000-0000000000f1"):
        return manifest_hold_usd(
            revision(channel_id),
            tenant_id="tenant",
            account_id=account_id,
            source_agent_id=source,
            source_agent_version=1,
        )

    path.write_text(
        json.dumps(
            [
                row(),
                # D3's tenant-wide channel row: a fixed plan no worker ever runs.
                row(platform="*", channel_id="*", hold_usd="0.25", **channel_only),
                row(tenant_id="other", hold_usd="0.01"),
            ]
        )
    )
    # The channel's own worker row wins; a channel's fixed plan never serves a worker.
    assert hold("fleet-test") == Decimal("0.49")
    assert hold("elsewhere") is None
    # A tenant-wide worker template serves workers in every channel; the pinned row still wins.
    path.write_text(
        json.dumps([row(), row(platform="*", channel_id="*", hold_usd="0.25", **unsourced)])
    )
    assert hold("fleet-test") == Decimal("0.49")
    assert hold("elsewhere") == Decimal("0.25")
    # Another agent's worker in #fleet-test gets the template, not the pinned row.
    assert hold("fleet-test", source="00000000-0000-0000-0000-0000000000f2") == Decimal("0.25")
    path.write_text(json.dumps([row(), row(platform="*", channel_id="*", hold_usd="0.25")]))
    assert hold("fleet-test") == Decimal("0.49")
    assert hold("elsewhere") == Decimal("0.25")
    # A "*" row pinned to the same agent serves only that agent.
    assert hold("elsewhere", source="00000000-0000-0000-0000-0000000000f2") is None
    # Two rows naming different fields are ambiguous: selection refuses, so no hold.
    path.write_text(json.dumps([row(), row(channel_id="*", account_id="acct", hold_usd="0.10")]))
    assert hold("fleet-test") is None
    assert hold("fleet-test", account_id="someone") == Decimal("0.49")
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    assert hold("fleet-test") is None


@pytest.mark.parametrize("reviewer", [False, True])
@pytest.mark.parametrize("captured", ["haiku55_effort"], indirect=True)
async def test_fleet_r9_plain_gemini_spawn_admits_the_derived_envelope_end_to_end(
    captured, tmp_path, monkeypatch, reviewer
):
    """Through FleetOrchestrator.spawn: the admitted worker carries the derived envelope,
    capped by the channel's manifest hold."""
    from types import SimpleNamespace
    from unittest.mock import create_autospec
    from uuid import uuid4

    from daimon.core.fleet_orchestration import FleetOrchestrator, FleetPorts, worker_trigger
    from daimon.core.neutral_resources import gemini_worker_cost_usd, task_worker_spec
    from mux.contracts.config import BackendConfig

    source, _, grant, fleet, policy, _ = captured
    flash = ModelRef(provider="gemini", id="gemini-3.8-flash")
    backend = backend_selection("gemini").model_copy(update={"model": flash.id})
    widened = grant.model_copy(update={"choices": (*grant.choices, (backend, flash))})
    root = compose(captured[:2] + (widened,) + captured[3:])
    large = "Real root instructions. " * 1000
    root = replace(root, spec=root.spec.model_copy(update={"system": large}), base_system=large)
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            [
                {
                    "profile": backend.profile,
                    "tenant_id": fleet.scope.tenant_id,
                    "platform": "*",
                    "channel_id": "*",
                    "account_id": "*",
                    "configuration_grant": {
                        "profiles": [backend.profile],
                        "models": [flash.id],
                        "thread_modes": [backend.thread_mode],
                        "requires": {},
                    },
                    "hold_usd": "0.12",
                    # A worker template: it holds for whichever agent the worker runs as.
                    "source_agent_id": "*",
                    "source_agent_version": "*",
                    "worker_materialization": True,
                }
            ]
        )
    )
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    ports = create_autospec(FleetPorts, instance=True)
    ports.capture_source.return_value = None
    admitted = []

    class Admitted(Exception):
        pass

    async def admit(parent, resolved, **kwargs):
        admitted.append(resolved)
        raise Admitted

    ports.admit.side_effect = admit
    owner = FleetOrchestrator(ports)
    triggers = []
    sized = owner._bounded_gemini  # pyright: ignore[reportPrivateUsage]

    def spy(*args, trigger, **kwargs):
        triggers.append(trigger)
        return sized(*args, trigger=trigger, **kwargs)

    monkeypatch.setattr(owner, "_bounded_gemini", spy)
    reviewed_id = None
    if reviewer:
        # A large reviewed result rides in the reviewer's trigger.
        reviewed_id = uuid4()
        target = SimpleNamespace(
            id=reviewed_id,
            reviewer_of=None,
            backend="openai",
            result=SimpleNamespace(digest="reviewed-digest"),
        )
        reviewed = SimpleNamespace(payload={"content": [{"text": "x" * 40_000}]})

        async def snapshot(*args):
            return SimpleNamespace(), (target,)

        async def result(*args):
            return reviewed

        monkeypatch.setattr(owner, "read_snapshot", snapshot)
        monkeypatch.setattr(owner, "_result", result)
    with pytest.raises(Admitted):
        await owner.spawn(
            root,
            policy,
            now=NOW,
            operation_key="W3",
            role="W3",
            backend=BackendConfig.model_validate(backend.model_dump()),
            limits=root.limits.model_copy(update={"max_turns": 1}),
            agent=task_worker_spec(root, role="W3", task="Compare X and Y.", model=flash.id),
            fill_token_limits=True,
            reviewer_of=reviewed_id,
        )
    (resolved,) = admitted
    (trigger,) = triggers
    # The trigger the envelope was sized for is exactly what dispatch will send.
    dispatched = worker_trigger(ports.admit.await_args.kwargs["reviewed_result"])
    assert trigger == dispatched and (reviewer == ("x" * 40_000 in dispatched))
    limits = resolved.limits
    assert limits.max_input_tokens == (
        len(resolved.spec.system.encode()) + len(dispatched.encode()) + 4096
    )
    worst = gemini_worker_cost_usd(flash.id, limits.max_input_tokens, limits.max_output_tokens)
    assert worst <= Decimal("0.12") <= limits.budget_usd


@pytest.mark.parametrize("pin", ["live", "live_upper", "stale"])
@pytest.mark.parametrize("captured", ["haiku55_effort"], indirect=True)
async def test_fleet_hold_comes_from_the_root_agents_live_pinned_row(
    captured, tmp_path, monkeypatch, pin
):
    """Through FleetOrchestrator.spawn: a plain worker is sized by the row its admission
    would select, the root agent's pin at its LIVE version; a stale pin is the template's."""
    from unittest.mock import create_autospec
    from uuid import UUID

    from daimon.core.fleet_orchestration import FleetOrchestrator, FleetPorts
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.neutral_resources import task_worker_spec
    from daimon.core.turn import runtime_manifest
    from mux.contracts.config import BackendConfig

    source, _, grant, fleet, policy, _ = captured
    flash = ModelRef(provider="gemini", id="gemini-3.8-flash")
    backend = backend_selection("gemini").model_copy(update={"model": flash.id})
    widened = grant.model_copy(update={"choices": (*grant.choices, (backend, flash))})
    root = compose(captured[:2] + (widened,) + captured[3:])
    large = "Real root instructions. " * 1000
    root = replace(root, spec=root.spec.model_copy(update={"system": large}), base_system=large)
    ((ref, revision),) = root.source_revisions
    source_id = str(derive_agent_uuid(tenant_id=UUID(fleet.scope.tenant_id), ma_agent_id=ref.id))
    common = {
        "profile": backend.profile,
        "tenant_id": fleet.scope.tenant_id,
        "account_id": "*",
        "configuration_grant": {
            "profiles": [backend.profile],
            "models": [flash.id],
            "thread_modes": [backend.thread_mode],
            "requires": {},
        },
        "worker_materialization": True,
    }
    template = {
        **common,
        "platform": "*",
        "channel_id": "*",
        "hold_usd": "0.12",
        "source_agent_id": "*",
        "source_agent_version": "*",
    }
    pinned = {
        **common,
        "platform": root.fleet.root.channel.platform,
        "channel_id": root.fleet.root.channel.channel_id,
        "hold_usd": "0.11",
        "source_agent_id": source_id.upper() if pin == "live_upper" else source_id,
        "source_agent_version": revision.local + (1 if pin == "stale" else 0),
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps([template, pinned]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    holds = []
    original = runtime_manifest.manifest_hold_usd

    def spy(*args, **kwargs):
        holds.append(original(*args, **kwargs))
        return holds[-1]

    monkeypatch.setattr(runtime_manifest, "manifest_hold_usd", spy)
    ports = create_autospec(FleetPorts, instance=True)
    ports.capture_source.return_value = None

    class Admitted(Exception):
        pass

    async def admit(parent, resolved, **kwargs):
        raise Admitted

    ports.admit.side_effect = admit
    with pytest.raises(Admitted):
        await FleetOrchestrator(ports).spawn(
            root,
            policy,
            now=NOW,
            operation_key="W4",
            role="W4",
            backend=BackendConfig.model_validate(backend.model_dump()),
            limits=root.limits.model_copy(update={"max_turns": 1}),
            agent=task_worker_spec(root, role="W4", task="Compare X and Y.", model=flash.id),
            fill_token_limits=True,
        )
    assert holds == [Decimal("0.12") if pin == "stale" else Decimal("0.11")]
