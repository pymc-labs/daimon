"""Admin ceilings meet actual captured source through the resource owner."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    fleet_root_permissions,
    resolve_worker_spec,
)
from daimon.core.sessions import project_session_resources
from daimon.core.stores import agent_memory_stores
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.turn.admission import AdmissionGrant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import MCPConnection, ResourceBinding
from mux.profiles import backend_selection

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
    native = ma_agent(
        id="root-source",
        name="helper",
        tenant_id=identity.tenant_id,
        model="claude-haiku-5-5",
        system="Protected source instruction",
        tools=[
            {
                "type": "custom",
                "name": "helper",
                "description": "Read facts",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        mcp_servers=[{"type": "url", "name": "private", "url": "https://private.invalid"}]
        if getattr(request, "param", None) == "mcp"
        else (),
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
            environment=ma_environment(tenant_id=identity.tenant_id),
            session_factory=sessions,
            tenant_id=identity.tenant_id,
            agent_uuid=source.agent_id,
            account_id=actor.account_id,
            resource_scope=scope,
            memory_read_only=True,
            mcp_settings=McpSettings(
                public_url="https://mcp.invalid/mcp", jwt_secret=SecretStr("offline-placeholder")
            )
            if getattr(request, "param", None) == "caller_credentials"
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
    assert permissions.limits.deadline == NOW + timedelta(seconds=120)
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


@pytest.mark.parametrize("captured", ("caller_credentials", "mcp"), indirect=True)
async def test_actual_projector_keeps_unsupported_main_credentials_and_mcp(captured):
    source, _, _, _, _, trace = captured
    assert source.resource_requirements or source.effective_spec.mcp_servers
    with pytest.raises(WorkerSpecRefused):
        compose(captured)
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
        "missing_memory",
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
            extension = ExtensionConfig(
                namespace="anthropic.agent_tools",
                version=1,
                value={
                    "tools": [{"type": "agent_toolset_20260401"}],
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
    elif fault == "missing_memory":
        source = source.model_copy(update={"memory_store_id": None})
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
