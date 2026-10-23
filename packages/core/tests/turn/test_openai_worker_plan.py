"""Declared worker plans use the real resolver, catalog and native readback."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from daimon.core.neutral_resources import (
    NeutralResourceHost,
    resolve_worker_spec,
)
from daimon.core.stores.neutral_catalog import CatalogConflict
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_deployment import OpenAIDeployment, build_openai_runtime
from daimon.core.turn.openai_host import driver
from daimon.testing.db import require_owned_sessionmaker
from mux.contracts.ids import SkillRef, ThreadRef
from mux.contracts.resources import (
    AgentPatch,
    EnvironmentSpec,
    MCPConnection,
    NetworkPolicy,
    ResourceBinding,
    ToolSpec,
    WorkspaceSource,
)
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError
from mux.testing.isolated_sdk import build_async_openai
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..test_fleet_permissions import POLICY, parent
from .native_counterpart_support import (
    Owned,
)
from .native_counterpart_support import (
    db_preflight as db_preflight,
)
from .native_counterpart_support import (
    owned as owned,
)
from .native_counterpart_support import (
    owned_database_preflight as owned_database_preflight,
)
from .test_openai_deployment import application as application
from .test_openai_deployment import manifest
from .test_openai_host import Wire as SessionWire
from .test_openai_host import openai_native_spend_cap


@pytest.fixture
def provider() -> str:
    return "openai"


@pytest.fixture
def db_session_factory(db_preflight, db_engine, db_clean):
    require_owned_sessionmaker(db_engine)
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest.fixture
async def target(owned: Owned, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple]:
    assert isinstance(owned.plan, OpenAIDeployment)
    root = parent("openai")
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=owned.request.scope,
            root=ThreadRef(
                channel=owned.request.admission.backend_revision.channel,
                thread_id=owned.request.thread_id,
            ),
            account_id=owned.request.session_account_id,
        ),
        spec=owned.retained.agent.spec.model_copy(
            update={
                "tools": (ToolSpec(name="bash", kind="builtin"),),
                "metadata": {"stamp": "root"},
            }
        ),
        base_system=owned.retained.agent.spec.system,
        limits=root.limits.model_copy(
            update={"deadline": owned.request.now() + timedelta(minutes=5)}
        ),
        environment=EnvironmentSpec(
            name="hosted", network=NetworkPolicy(mode="none"), sources=(), packages={}
        ),
    )
    worker = resolve_worker_spec(
        root,
        root.spec.model_copy(update={"declared_drops": ("memory",)}),
        policy=POLICY,
        now=owned.request.now(),
    )
    scope = worker.permissions.fleet.scope
    agent = owned.retained.agent.model_copy(update={"spec": worker.spec})
    row = await owned.resources.catalog.put_agent(
        scope, owned.retained.catalog_id, agent, expected_revision=owned.retained.local_revision
    )
    owned.wire.agent["instructions"] = worker.spec.system
    owned.wire.agent["metadata"]["stamp"] = "root"
    owned.wire.environment["packages"] = {}
    plan = owned.plan.model_copy(update={"counterpart_catalog_revision": row.local_revision})
    request = replace(
        owned.request,
        worker=worker,
        scope=scope,
        counterpart=row,
        deps=replace(owned.request.deps, agent_backends=True),
    )
    manifest(monkeypatch, owned.manifest_path, [plan.model_dump(mode="json")])
    async with build_async_openai(
        transport=httpx.MockTransport(owned.wire.handle),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        backend = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda checked, kind, identity: (
                checked == scope
                and (kind, identity)
                in {("agent", "owned-agent"), ("environment", "owned-environment")}
            ),
        )
        resources = NeutralResourceHost(
            backend,
            request.deps.sessionmaker,
            scope=scope,
            account_scope_id="project",
            operation_key="worker-target-read",
        )
        owned.wire.requests.clear()
        yield request, worker, plan, resources, owned


async def test_checked_worker_readback_and_runtime_consumption(target: tuple) -> None:
    request, worker, plan, resources, owned = target
    checked = await plan.read_counterpart(request, source=owned.source, resources=resources)
    assert checked.agent.spec == worker.spec
    assert "no persistent memory" in checked.agent.spec.system
    assert owned.wire.requests and all(r.method == "GET" for r in owned.wire.requests)
    runtime = build_openai_runtime(request)
    consumed = await runtime.session_plan(replace(request, counterpart=checked))
    assert consumed.agent == checked.agent.ref
    assert consumed.environment.id == plan.environment_id
    assert consumed.resources == () and not consumed.extensions


@pytest.mark.parametrize(
    "failure",
    (
        "memory",
        "sandbox",
        "tools",
        "skills",
        "mcp",
        "files",
        "repository",
        "implicit_bash",
        "mount",
        "scope",
        "network",
        "files_unknown",
        "packages_unknown",
        "backend",
    ),
)
async def test_unrepresentable_worker_refuses_before_native_read(
    target: tuple, failure: str
) -> None:
    request, worker, plan, resources, owned = target
    if failure in ("memory", "sandbox", "tools"):
        drops = () if failure == "memory" else ("memory", failure)
        worker = replace(worker, spec=worker.spec.model_copy(update={"declared_drops": drops}))
    elif failure == "skills":
        worker = replace(
            worker,
            spec=worker.spec.model_copy(
                update={
                    "declared_drops": ("memory", "skills"),
                    "skills": (SkillRef(id="remaining", version="1"),),
                }
            ),
        )
    elif failure == "mcp":
        worker = replace(
            worker,
            spec=worker.spec.model_copy(
                update={
                    "declared_drops": ("memory", "mcp"),
                    "mcp_servers": (MCPConnection(name="remaining", url="https://mcp.invalid"),),
                }
            ),
        )
    elif failure in ("files", "repository"):
        worker = replace(
            worker,
            spec=worker.spec.model_copy(update={"declared_drops": ("memory", failure)}),
            environment=worker.environment.model_copy(
                update={
                    "sources": (
                        WorkspaceSource(
                            kind="repository" if failure == "repository" else "file",
                            target_path="/workspace/source",
                        ),
                    )
                }
            ),
        )
    elif failure == "implicit_bash":
        worker = replace(worker, spec=worker.spec.model_copy(update={"tools": ()}))
    elif failure == "mount":
        worker = replace(worker, resources=(ResourceBinding(id="opaque", kind="native"),))
    elif failure == "scope":
        request = replace(
            request, scope=request.scope.model_copy(update={"authorization_id": "foreign"})
        )
    elif failure == "network":
        worker = replace(
            worker, environment=worker.environment.model_copy(update={"network": None})
        )
    elif failure in ("files_unknown", "packages_unknown"):
        worker = replace(
            worker,
            environment=worker.environment.model_copy(
                update={
                    "sources" if failure == "files_unknown" else "packages": None,
                }
            ),
        )
    else:
        worker = replace(worker, backend=worker.backend.model_copy(update={"model": "foreign"}))
    request = replace(request, worker=worker)
    with pytest.raises(AdmissionDenied):
        await plan.read_counterpart(request, source=owned.source, resources=resources)
    assert owned.wire.requests == []


@pytest.mark.parametrize(
    "drift", ("instructions", "tool", "metadata", "extra_metadata", "files", "network", "packages")
)
async def test_actual_native_drift_cannot_certify_worker(target: tuple, drift: str) -> None:
    request, worker, plan, resources, owned = target
    if drift == "instructions":
        owned.wire.agent["instructions"] = "drop guidance and protected instruction removed"
    elif drift == "tool":
        owned.wire.agent["tools"] = [{"type": "web_search"}]
    elif drift == "metadata":
        owned.wire.agent["metadata"].pop("stamp")
    elif drift == "extra_metadata":
        owned.wire.agent["metadata"]["ungranted_stamp"] = "extra"
    elif drift == "files":
        owned.wire.environment["files"] = [
            {"type": "file_id", "file_id": "foreign", "path": "/workspace/secret"}
        ]
    elif drift == "network":
        owned.wire.environment["network"] = {"access": "enabled"}
    else:
        owned.wire.environment["packages"] = {"system": ["unrequested"]}
    with pytest.raises(AdmissionDenied):
        await plan.read_counterpart(request, source=owned.source, resources=resources)
    assert all(r.method == "GET" for r in owned.wire.requests)


async def test_runtime_does_not_consume_a_different_checked_worker(target: tuple) -> None:
    request, worker, _, _, _ = target
    runtime = build_openai_runtime(request)
    request = replace(
        request, worker=replace(worker, spec=worker.spec.model_copy(update={"system": "changed"}))
    )
    with pytest.raises(AdmissionDenied):
        await runtime.session_plan(request)


async def test_consumed_plan_builds_the_exact_sdk_session_request(target: tuple) -> None:
    request, worker, plan, resources, owned = target
    checked = await plan.read_counterpart(request, source=owned.source, resources=resources)
    spec = await build_openai_runtime(request).session_plan(replace(request, counterpart=checked))
    posts: list[httpx.Request] = []
    native = SessionWire().session()
    native["agent"]["id"] = "owned-agent"

    def handle(call: httpx.Request) -> httpx.Response:
        if call.method == "POST" and call.url.path == "/v1/agents/sessions":
            posts.append(call)
            return httpx.Response(200, json=native)
        return owned.wire.handle(call)

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        runtime = replace(
            build_openai_runtime(request), transport_factory=lambda config, scope: SDKTransport(sdk)
        )
        backend = driver(runtime, request.admission.backend_revision, request.scope)
        await backend.sessions.create(request.scope, spec, key="owned-worker-create")
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body["agent_id"] == checked.agent.ref.id
    assert body["environment"] == {
        "type": "openai_hosted",
        "container_size": "small",
        "environment_template_id": plan.environment_id,
    }
    assert body["agent"] == {"model": "gpt-6-luna", "multi_agent": {"enabled": False}}
    assert "resources" not in body and "vault_ids" not in body and "spend_control" not in body
    assert "declared_drops" not in body


@pytest.mark.parametrize("clone", (False, True))
async def test_resource_owner_publishes_exact_fresh_or_clone_worker(
    target: tuple, clone: bool
) -> None:
    request, selected, plan, resources, owned = target
    source = await resources.read(plan.counterpart_catalog_id)
    root = parent("openai")
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=owned.request.scope,
            root=ThreadRef(
                channel=request.admission.backend_revision.channel, thread_id=request.thread_id
            ),
            account_id=request.session_account_id,
        ),
        spec=selected.spec,
        base_system=selected.spec.system,
        environment=selected.environment,
        limits=selected.limits,
        source_agents=(source.agent.ref,),
        source_revisions=((source.agent.ref, source.agent.revision),),
    )
    resolved = resolve_worker_spec(
        root,
        source.agent if clone else root.spec.model_copy(update={"name": "actual-worker"}),
        overrides=AgentPatch(name="actual-worker") if clone else None,
        policy=POLICY,
        now=request.now(),
    )
    calls: list[httpx.Request] = []
    native: dict = {}

    def handle(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        assert call.headers["OpenAI-Beta"] == "agents=v1"
        if call.method == "POST" and call.url.path == "/v1/agents":
            assert call.headers["Idempotency-Key"] == "owned-publication"
            native.update(json.loads(call.content), id="acknowledged-worker", created_at=1791792000)
            return httpx.Response(200, json=native)
        assert call.method == "GET" and call.url.path == "/v1/agents/acknowledged-worker"
        return httpx.Response(200, json=native)

    async with build_async_openai(
        transport=httpx.MockTransport(handle),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        backend = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda scope, kind, identity: (
                scope == resolved.permissions.fleet.scope
                and kind == "agent"
                and identity in (None, "acknowledged-worker")
            ),
        )
        host = NeutralResourceHost(
            backend,
            request.deps.sessionmaker,
            scope=resolved.permissions.fleet.scope,
            account_scope_id="project",
            operation_key="owned-publication",
        )
        row = await host.publish(
            "owned-worker", resolved.spec, expected_revision=0, key="owned-publication"
        )
        assert await host.read("owned-worker") == row
        assert row.agent.ref.id == "acknowledged-worker" and row.agent.spec == resolved.spec
        assert row.agent.ref != source.agent.ref
        assert row.local_revision == 1 and "no persistent memory" in row.agent.spec.system
        assert native["model"] == "gpt-6-luna" and native["instructions"] == resolved.spec.system
        assert native["tools"] == [] and "declared_drops" not in native
        calls.clear()
        with pytest.raises(CatalogConflict):
            await host.publish(
                "owned-worker", resolved.spec, expected_revision=0, key="stale-publication"
            )
        assert calls == []
    assert await resources.entry(source.catalog_id) == source


async def test_resource_owner_lost_native_ack_is_not_a_published_target(target: tuple) -> None:
    request, selected, _, _, _ = target
    calls: list[httpx.Request] = []

    def lose_ack(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        assert call.method == "POST" and call.url.path == "/v1/agents"
        raise httpx.ReadError("offline lost acknowledgement", request=call)

    async with build_async_openai(
        transport=httpx.MockTransport(lose_ack),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        backend = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            authorization=lambda scope, kind, identity: scope == request.scope and kind == "agent",
        )
        host = NeutralResourceHost(
            backend,
            request.deps.sessionmaker,
            scope=request.scope,
            account_scope_id="project",
            operation_key="lost-publication",
        )
        with pytest.raises(ProviderError):
            await host.publish(
                "unacknowledged-worker", selected.spec, expected_revision=0, key="lost-publication"
            )
        assert await host.catalog.get_agent(request.scope, "unacknowledged-worker") is None
    assert len(calls) == 1


def test_worker_materialization_is_strict_opt_in_without_default_serialization_change(
    target: tuple,
) -> None:
    _, _, plan, _, _ = target
    assert plan.worker_materialization is False
    assert "worker_materialization" not in plan.model_dump(mode="json")
    for value in (1, "true", None):
        with pytest.raises(ValidationError):
            OpenAIDeployment.model_validate(
                {**plan.model_dump(mode="json"), "worker_materialization": value}
            )


@pytest.mark.parametrize("enabled", (False, True))
async def test_materialization_capability_does_not_make_counterpart_read_mutate(
    target: tuple, enabled: bool
) -> None:
    request, _, plan, resources, owned = target
    if enabled:
        plan = OpenAIDeployment.model_validate(
            {
                **plan.model_dump(
                    mode="json",
                    exclude={"agent_id", "counterpart_catalog_id", "counterpart_catalog_revision"},
                ),
                "worker_materialization": True,
            }
        )
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    if enabled:
        with pytest.raises(AdmissionDenied):
            await plan.read_counterpart(request, source=owned.source, resources=resources)
        assert owned.wire.requests == []
    else:
        row = await plan.read_counterpart(request, source=owned.source, resources=resources)
        assert row == request.counterpart
        assert owned.wire.requests and all(call.method == "GET" for call in owned.wire.requests)


@pytest.fixture(params=(False, True), ids=("fresh", "clone"))
async def claimed_openai_worker(
    owned: Owned, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, native_budget
):
    from daimon.core.authz import Surface
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import access_policy, fleet, mux_state, tenant_ledger, tenants
    from daimon.core.turn.admission import AdmissionGrant
    from daimon.core.turn.runtime_manifest import capture_preparation_source
    from mux.contracts.config import BackendConfig
    from mux.contracts.resources import ProviderBinding

    from ..test_fleet_budget import tariffs

    selected = owned.request
    native_source = selected.admission.agent.model_copy(
        update={
            "metadata": {
                MA_METADATA_KEY_TENANT: str(selected.tenant_id),
                MA_METADATA_KEY_NAME: selected.admission.config.agent_name,
            }
        }
    )
    native_environment = type(selected.admission.environment).model_validate(
        {
            **selected.admission.environment.model_dump(mode="json"),
            "name": "hosted",
            "description": "",
            "metadata": {},
            "scope": None,
            "config": {
                "type": "cloud",
                "networking": {"type": "unrestricted"},
                "packages": {"apt": [], "pip": [], "npm": [], "cargo": [], "gem": [], "go": []},
            },
        }
    )
    environment = EnvironmentSpec(
        name="hosted",
        description="",
        metadata={},
        network=NetworkPolicy(mode="unrestricted"),
        packages={"system": (), "python": (), "npm": ()},
        sources=(),
    )
    selected = replace(
        selected,
        admission=replace(selected.admission, agent=native_source, environment=native_environment),
        deps=replace(
            selected.deps,
            agent_backends=True,
            agent_github_app=None,
            state_store=mux_state.PostgresStateStore(selected.deps.sessionmaker),
        ),
    )
    root = parent("openai")
    spec = owned.retained.agent.spec.model_copy(
        update={"tools": (ToolSpec(name="bash", kind="builtin"),), "skills": ()}
    )
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=selected.scope,
            root=ThreadRef(channel=selected.admission.backend_revision.channel, thread_id="thread"),
            account_id=selected.session_account_id,
        ),
        agent_id=derive_agent_uuid(tenant_id=selected.tenant_id, ma_agent_id=native_source.id),
        spec=spec,
        base_system=spec.system,
        environment=environment,
        limits=root.limits.model_copy(update={"deadline": selected.now() + timedelta(minutes=2)}),
    )
    if native_budget is not None:
        openai_native_spend_cap(monkeypatch)
        root = replace(
            root,
            limits=type(root.limits).model_validate(
                root.limits.model_dump() | {"native_budget_usd": native_budget, "max_turns": 1}
            ),
        )
    sm = selected.deps.sessionmaker
    async with sm.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=selected.tenant_id, policy=POLICY)
        await tenants.set_funding_mode(db, tenant_id=selected.tenant_id, funding_mode="prepaid")
        await tenant_ledger.insert_entry(
            db,
            tenant_id=selected.tenant_id,
            delta_usd=Decimal("10"),
            reason="trial_credit",
            idempotency_key="openai-worker-credit",
        )
        await mux_state.put_config_revision(db, selected.admission.backend_revision, origin="agent")
        binding = ProviderBinding(
            id="openai-orchestrator-binding",
            thread=root.fleet.root,
            legacy_account_id=str(root.fleet.account_id),
            provider="openai",
            profile=root.backend_grants[0].profile,
            native_refs={"session": "openai-orchestrator-session"},
            generation=1,
            config_revision=selected.admission.backend_revision.local,
        )
        await mux_state.put_binding(db, binding, expected_generation=0)
        run = await fleet.create_run(
            db,
            tenant_id=selected.tenant_id,
            orchestrator_account_id=root.fleet.account_id,
            orchestrator_session_id="openai-orchestrator-session",
            orchestrator_binding_id=binding.id,
            platform=selected.platform,
            channel_id=root.fleet.root.channel.channel_id,
            thread_id=root.fleet.root.thread_id,
            operation_key="openai-worker-run",
            total_budget_usd=root.limits.budget_usd,
            deadline_at=root.limits.deadline,
            max_workers=2,
            created_by="host",
        )
    root = replace(root, fleet=replace(root.fleet, run_id=run.id))
    source = await capture_preparation_source(selected)
    assert source.source is not None
    if request.param:
        spec = spec.model_copy(update={"metadata": source.source.spec.metadata})
        root = replace(
            root,
            spec=spec,
            source_agents=(source.source.ref,),
            source_revisions=((source.source.ref, source.source.revision),),
        )
    resolved = resolve_worker_spec(
        root,
        source.source
        if request.param
        else spec.model_copy(update={"name": "fresh-worker", "declared_drops": ("memory",)}),
        overrides=AgentPatch(
            name="clone-worker",
            model=spec.model,
            system=spec.system,
            tools=spec.tools,
            skills=(),
            mcp_servers=(),
            extensions=None,
            declared_drops=("memory",),
        )
        if request.param
        else None,
        policy=POLICY,
        now=selected.now(),
        backend=BackendConfig.model_validate(
            selected.admission.backend_revision.model_dump(include=set(BackendConfig.model_fields))
        ),
        environment=environment,
        captured_source=source,
    )
    async with sm.begin() as db:
        await fleet.add_worker(
            db,
            tenant_id=selected.tenant_id,
            run_id=run.id,
            ordinal=0,
            role="researcher",
            spec=resolved.spec.model_dump(mode="json"),
            spec_digest=resolved.digest,
            backend=resolved.backend.backend,
            profile=resolved.backend.profile,
            model=resolved.spec.model.id,
            budget_usd=resolved.limits.budget_usd,
            deadline_at=resolved.limits.deadline,
            max_turns=resolved.limits.max_turns,
        )
    pool = fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=1)
    worker = await pool.claim(sm, tenant_id=selected.tenant_id, run_id=run.id)
    assert worker is not None and worker.status == "starting"
    grant = AdmissionGrant(
        tenant_id=selected.tenant_id,
        subject=root.subject,
        surface=Surface.CHANNEL,
        turn_place=root.fleet.place,
        agent=root.agent,
        run_place=root.fleet.place,
        channel_id=root.fleet.root.channel.channel_id,
        thread_id=root.fleet.root.thread_id,
        is_dm=False,
    )
    selected = replace(
        selected,
        scope=resolved.permissions.fleet.scope,
        worker=resolved,
        admission=replace(selected.admission, grant=grant),
    )
    plan = OpenAIDeployment.model_validate(
        {
            **owned.plan.model_dump(
                mode="json",
                exclude={"agent_id", "counterpart_catalog_id", "counterpart_catalog_revision"},
            ),
            "worker_materialization": True,
        }
    )
    manifest(monkeypatch, owned.manifest_path, [plan.model_dump(mode="json")])
    original = os.environ.get
    monkeypatch.setattr(
        os.environ,
        "get",
        lambda name, default=None: (
            "offline" if name == plan.api_key_env else original(name, default)
        ),
    )
    calls: list[httpx.Request] = []
    agents: dict[str, dict] = {owned.retained.agent.ref.id: owned.wire.agent}
    wire = SessionWire()
    wire.expected_spend_limit = 1 if native_budget is not None else None

    def native(call: httpx.Request) -> httpx.Response:
        calls.append(call)
        path = call.url.path
        if path == "/v1/agents" and call.method == "POST":
            if wire.fail_create:
                raise httpx.ReadError("offline lost agent acknowledgement", request=call)
            agents["allocated-worker"] = {
                **json.loads(call.content),
                "id": "allocated-worker",
                "created_at": 1791792000,
            }
            return httpx.Response(200, json=agents["allocated-worker"])
        if path in {"/v1/agents/" + key for key in agents}:
            assert call.method == "GET"
            return httpx.Response(200, json=agents[path.rsplit("/", 1)[1]])
        if path == "/v1/agents/environments/templates/owned-environment":
            assert call.method == "GET"
            return httpx.Response(
                200,
                json={
                    **owned.wire.environment,
                    "network": {"access": "enabled"},
                    "packages": {"system": [], "python": [], "npm": []},
                },
            )
        if path == "/v1/agents/sessions" and call.method == "POST":
            raw = wire.session()
            raw["agent"]["id"] = "allocated-worker"
            return httpx.Response(200, json=raw)
        response = wire.handle(call)
        for turn in wire.turns:
            turn["agent_id"] = "allocated-worker"
        if (
            call.method == "GET"
            and response.status_code == 200
            and path.endswith("native-openai-session")
        ):
            raw = json.loads(response.content)
            raw["agent"]["id"] = "allocated-worker"
            return httpx.Response(200, json=raw)
        return response

    async with build_async_openai(
        transport=httpx.MockTransport(native),
        api_key="offline",
        base_url="https://openai.invalid/v1",
    ) as sdk:
        import mux.drivers.openai as native_transport

        monkeypatch.setattr(
            native_transport, "configured_transport", lambda **kwargs: SDKTransport(sdk)
        )
        try:
            yield selected, root, worker, tariffs(root), owned, calls, wire
        finally:
            pool.release()


async def test_openai_fresh_or_clone_worker_uses_actual_dispatch_allocation_binding_and_journal(
    claimed_openai_worker,
) -> None:
    from daimon.core.fleet import spawn_operation_key
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import mux_state
    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    request, root, worker, prices, owned, calls, _ = claimed_openai_worker
    result = await run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Complete the assigned task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.id == worker.id and result.status == "succeeded"
    assert result.turns_used == 1 and result.result is not None
    assert result.result.excerpt == "answer turn-1"
    posts = [call for call in calls if call.method == "POST"]
    assert [call.url.path for call in posts] == [
        "/v1/agents",
        "/v1/agents/sessions",
        "/v1/agents/sessions/native-openai-session/events",
    ]
    assert (
        posts[0].headers["Idempotency-Key"]
        == spawn_operation_key(worker.run_id, 0) + ":agent_create:0"
    )
    assert (
        posts[1].headers["Idempotency-Key"]
        == spawn_operation_key(worker.run_id, 0) + ":session_create:0"
    )
    assert json.loads(posts[1].content)["agent_id"] == "allocated-worker"
    async with request.deps.sessionmaker() as db:
        journal = await mux_state.read_events(
            db, result.result.session_id, after=result.result.sequence - 1, limit=1
        )
    assert len(journal) == 1 and journal[0].type == "agent.message"
    assert journal[0].id == f"fleet:{worker.id}:result"
    recorded = await request.deps.state_store.read_events(result.result.session_id, after=-1)
    messages = [event for event in recorded if event.type == "agent.message"]
    assert any(
        event.id == "openai:item:turn-1:answer" and event.native.event_id == "turn-1:answer"
        for event in messages
    ), "the accepted display identity must retain the true native stream provenance"
    await drain_outcomes()
    async with request.deps.sessionmaker() as db:
        outcomes = await list_for_tenant(db, request.tenant_id)
    assert any(
        row.origin == "worker"
        and row.session_id == result.result.session_id
        and row.reason.value == "completed"
        for row in outcomes
    )
    assert await owned.resources.entry(owned.retained.catalog_id) == owned.retained


@pytest.mark.parametrize("fault", ("memory", "opaque_environment", "cancelled", "expired"))
async def test_openai_worker_refuses_unsupported_or_revoked_authority_before_native_io(
    claimed_openai_worker, fault
) -> None:
    from daimon.core.errors import TurnError
    from daimon.core.headless_runner import run_worker
    from daimon.core.neutral_resources import WorkerSpecRefused

    request, root, worker, prices, _, calls, _ = claimed_openai_worker
    cancel = asyncio.Event()
    if fault == "memory":
        spec = request.worker.spec.model_copy(update={"declared_drops": ()})
        worker = replace(worker, spec=spec.model_dump(mode="json"))
    elif fault == "opaque_environment":
        environment = request.admission.environment.model_copy(
            update={
                "config": request.admission.environment.config.model_copy(update={"opaque": True})
            }
        )
        request = replace(request, admission=replace(request.admission, environment=environment))
    elif fault == "cancelled":
        cancel.set()
    else:
        request = replace(request, deadline=request.now())
    with pytest.raises((WorkerSpecRefused, AdmissionDenied, asyncio.CancelledError, TurnError)):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Refused task",
            prices=prices,
            cancel=cancel,
        )
    assert calls == []
    if fault == "cancelled":
        from daimon.core.stores import fleet

        _, rows = await fleet.run_status(
            request.deps.sessionmaker,
            tenant_id=worker.tenant_id,
            run_id=worker.run_id,
        )
        ended = next(row for row in rows if row.id == worker.id)
        assert ended.status == "failed" and ended.error_code == "preparation_refused"
        assert ended.reserved_usd > 0 and ended.binding_id is None and ended.turns_used == 0


async def test_openai_worker_lost_agent_ack_is_retained_without_reallocation(
    claimed_openai_worker,
) -> None:
    from daimon.core.fleet import spawn_operation_key
    from daimon.core.headless_runner import run_worker
    from daimon.core.turn.persistence import UncertainSend

    request, root, worker, prices, owned, calls, wire = claimed_openai_worker
    wire.fail_create = True
    for _ in range(2):
        with pytest.raises((ProviderError, UncertainSend, AdmissionDenied)):
            await run_worker(
                request,
                parent=root,
                worker=worker,
                trigger_message="Uncertain task",
                prices=prices,
                cancel=asyncio.Event(),
            )
        posts = [call for call in calls if call.method == "POST"]
        assert len(posts) == 1 and posts[0].url.path == "/v1/agents"
    key = spawn_operation_key(worker.run_id, 0)
    claim = await request.deps.state_store.get_operation(request.scope, key + ":agent_create:0")
    assert claim is not None and claim.operation.status == "outcome_unknown"
    assert await owned.resources.catalog.get_agent(request.scope, key + ":agent") is None


async def test_worker_native_mutations_reuse_the_owned_guard(claimed_openai_worker, monkeypatch):
    from daimon.core.headless_runner import run_worker
    from daimon.core.turn import openai_host

    request, root, worker, prices, _, _, _ = claimed_openai_worker
    build = request.deps.turn_runtime_factory
    assert build is not None
    create = openai_host._create
    checks = {"agent": 0, "session": 0}

    def instrument_build(request):
        runtime = build(request)
        materialize = runtime.materialize_worker
        assert materialize is not None

        async def instrument_materialize(preparation):
            guard = preparation.before_native
            assert guard is not None

            async def counted():
                checks["agent"] += 1
                await guard()

            return await materialize(replace(preparation, before_native=counted))

        return replace(runtime, materialize_worker=instrument_materialize)

    async def instrument_create(request, *args):
        guard = request.before_native
        assert guard is not None

        async def counted():
            checks["session"] += 1
            await guard()

        return await create(replace(request, before_native=counted), *args)

    request = replace(request, deps=replace(request.deps, turn_runtime_factory=instrument_build))
    monkeypatch.setattr(openai_host, "_create", instrument_create)
    result = await run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Complete the task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded"
    # Publication checks authority before catalog work and before native claim;
    # session mutation checks it once at its native claim boundary.
    assert checks == {"agent": 2, "session": 1}


@pytest.mark.parametrize("boundary", ["agent", "session"])
async def test_worker_mutation_guard_refuses_cancellation_after_readback(
    claimed_openai_worker, monkeypatch, boundary
):
    from daimon.core.headless_runner import run_worker
    from daimon.core.turn import openai_host

    request, root, worker, prices, _, calls, _ = claimed_openai_worker
    cancel = asyncio.Event()
    verify = OpenAIDeployment.verify_environment
    create = openai_host._create

    async def verify_then_cancel(self, *args, **kwargs):
        await verify(self, *args, **kwargs)
        cancel.set()

    async def create_after_cancel(*args, **kwargs):
        cancel.set()
        return await create(*args, **kwargs)

    if boundary == "agent":
        monkeypatch.setattr(OpenAIDeployment, "verify_environment", verify_then_cancel)
    else:
        monkeypatch.setattr(openai_host, "_create", create_after_cancel)
    with pytest.raises(asyncio.CancelledError):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Refused task",
            prices=prices,
            cancel=cancel,
        )
    posts = [call.url.path for call in calls if call.method == "POST"]
    assert posts == ([] if boundary == "agent" else ["/v1/agents"])


@pytest.mark.parametrize("native_budget", [Decimal("0.01")])
async def test_native_worker_budget_reaches_real_request_and_one_input(claimed_openai_worker):
    from daimon.core.fleet import FleetConflict
    from daimon.core.headless_runner import run_worker

    request, root, worker, prices, _, calls, _ = claimed_openai_worker
    assert request.worker.limits.max_turns == 1
    assert 0 < (request.worker.limits.deadline - request.now()).total_seconds() <= 120
    result = await run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Bounded task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded" and result.turns_used == result.max_turns == 1
    created = [
        call for call in calls if call.method == "POST" and call.url.path == "/v1/agents/sessions"
    ]
    assert len(created) == 1
    assert json.loads(created[0].content)["spend_control"] == {"limit": 1}
    with pytest.raises(FleetConflict):
        await run_worker(
            request,
            parent=root,
            worker=result,
            trigger_message="Refused second input",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert sum(call.method == "POST" and call.url.path.endswith("/events") for call in calls) == 1


@pytest.mark.parametrize("native_budget", [Decimal("0.01")])
@pytest.mark.parametrize("echo", [None, True, "1", 1.0, 2])
async def test_native_worker_budget_mismatch_never_dispatches(
    claimed_openai_worker, monkeypatch, echo
):
    from daimon.core.errors import TurnError
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import fleet
    from mux.errors import ContinuityLost

    request, root, worker, prices, _, calls, wire = claimed_openai_worker
    original = wire.session

    def changed():
        raw = original()
        if echo is None:
            raw.pop("spend_control", None)
        else:
            raw["spend_control"] = {"limit": echo}
        return raw

    monkeypatch.setattr(wire, "session", changed)
    with pytest.raises((ContinuityLost, TurnError, AdmissionDenied)):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Refused cap",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert not any(call.method == "POST" and call.url.path.endswith("/events") for call in calls)
    async with request.deps.sessionmaker.begin() as db:
        _, rows = await fleet.run_status(db, tenant_id=request.tenant_id, run_id=worker.run_id)
    persisted = next(row for row in rows if row.id == worker.id)
    assert persisted.turns_used == 0 and persisted.reserved_usd == worker.budget_usd


@pytest.mark.parametrize("native_budget", [Decimal("0.01")])
async def test_budgeted_worker_lost_ack_keeps_one_claim(claimed_openai_worker, monkeypatch):
    from daimon.core.errors import TurnError
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import fleet

    request, root, worker, prices, _, calls, _ = claimed_openai_worker
    request = replace(request, deadline=request.now() + timedelta(seconds=5))
    original = httpx.MockTransport.handle_async_request

    async def lost_ack(transport, call):
        response = await original(transport, call)
        if call.method == "POST" and call.url.path.endswith("/events"):
            raise httpx.ReadError("offline lost acknowledgment", request=call)
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", lost_ack)
    with pytest.raises(TurnError):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Uncertain input",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert sum(call.method == "POST" and call.url.path.endswith("/events") for call in calls) == 1
    async with request.deps.sessionmaker.begin() as db:
        _, rows = await fleet.run_status(db, tenant_id=request.tenant_id, run_id=worker.run_id)
    persisted = next(row for row in rows if row.id == worker.id)
    assert persisted.turns_used == 1 and persisted.reserved_usd == worker.budget_usd


@pytest.mark.parametrize("native_budget", [Decimal("0.01")])
async def test_native_worker_budget_rejection_never_falls_back_uncapped(
    claimed_openai_worker, monkeypatch
):
    from daimon.core.errors import TurnError
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import fleet

    request, root, worker, prices, _, calls, _ = claimed_openai_worker
    original = httpx.MockTransport.handle_async_request

    async def rejected(transport, call):
        response = await original(transport, call)
        if call.method == "POST" and call.url.path == "/v1/agents/sessions":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "unsupported native budget",
                        "type": "invalid_request_error",
                    }
                },
            )
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", rejected)
    with pytest.raises((ProviderError, AdmissionDenied, TurnError)):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="No fallback",
            prices=prices,
            cancel=asyncio.Event(),
        )
    posts = [
        call for call in calls if call.method == "POST" and call.url.path == "/v1/agents/sessions"
    ]
    assert len(posts) == 1 and json.loads(posts[0].content)["spend_control"] == {"limit": 1}
    assert not any(call.method == "POST" and call.url.path.endswith("/events") for call in calls)
    async with request.deps.sessionmaker.begin() as db:
        _, rows = await fleet.run_status(db, tenant_id=request.tenant_id, run_id=worker.run_id)
    persisted = next(row for row in rows if row.id == worker.id)
    assert persisted.turns_used == 0 and persisted.reserved_usd == worker.budget_usd


@pytest.mark.parametrize("native_budget", [Decimal("0.01")])
async def test_native_worker_budget_conflict_refuses_before_transport(
    claimed_openai_worker, monkeypatch
):
    request, _, _, _, owned, calls, _ = claimed_openai_worker
    configured = json.loads(owned.manifest_path.read_text())
    configured[0]["spend_limit_usd_cents"] = 2
    owned.manifest_path.write_text(json.dumps(configured))
    calls.clear()
    with pytest.raises(AdmissionDenied):
        build_openai_runtime(request)
    assert calls == []


async def test_native_budget_compiler_is_decimal_context_independent(target, monkeypatch):
    from decimal import localcontext

    openai_native_spend_cap(monkeypatch)

    from daimon.core.neutral_resources import WorkerLimits

    request, worker, _, _, _ = target
    limits = WorkerLimits.model_validate(
        worker.limits.model_dump()
        | {
            "native_budget_usd": Decimal("1234567.89"),
            "budget_usd": Decimal("2000000"),
            "max_turns": 1,
            "deadline": request.now() + timedelta(seconds=120),
        }
    )
    candidate = replace(
        request,
        worker=replace(
            worker, limits=limits, permissions=replace(worker.permissions, limits=limits)
        ),
    )
    with localcontext() as context:
        context.prec = 6
        runtime = build_openai_runtime(candidate)
    assert runtime.controls.spend_limit_usd_cents == 123456789
    assert runtime.controls.require_spend_control_echo is True


@pytest.mark.parametrize("native_budget", [Decimal("0.01")])
async def test_budgeted_worker_can_cancel_after_its_one_input(claimed_openai_worker, monkeypatch):
    from daimon.core.errors import TurnError
    from daimon.core.fleet import FleetConflict
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import fleet

    from .test_openai_host import Stream

    request, root, worker, prices, _, calls, wire = claimed_openai_worker
    cancel = asyncio.Event()
    original_session = wire.session

    def active_session():
        raw = original_session()
        if wire.turns and not wire.cancelled:
            raw["status"] = "in_progress"
        return raw

    async def blocked(stream):
        await stream.wire.sent.wait()
        turn = stream.wire.turns[-1]
        turn["status"] = "in_progress"
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "agent.session.turn.in_progress",
                    "event_id": turn["id"] + ":running",
                    "session_id": turn["session_id"],
                    "turn": turn,
                }
            )
            + "\n\n"
        ).encode()
        cancel.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(wire, "session", active_session)
    monkeypatch.setattr(Stream, "__aiter__", blocked)
    with pytest.raises((FleetConflict, TurnError, asyncio.CancelledError)):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Cancellable task",
            prices=prices,
            cancel=cancel,
        )
    assert wire.cancelled
    bodies = [
        json.loads(call.content)
        for call in calls
        if call.method == "POST" and call.url.path.endswith("/events")
    ]
    assert sum(body["events"][0]["type"] != "agent.session.input.cancel" for body in bodies) == 1
    assert sum(body["events"][0]["type"] == "agent.session.input.cancel" for body in bodies) == 1
    async with request.deps.sessionmaker.begin() as db:
        _, rows = await fleet.run_status(db, tenant_id=request.tenant_id, run_id=worker.run_id)
    assert next(row for row in rows if row.id == worker.id).turns_used == 1


@pytest.mark.parametrize("native_budget", [Decimal("0.01")])
@pytest.mark.parametrize("stage", ["ack", "get"])
@pytest.mark.parametrize("echo", [None, True, "1", 1.0, 2])
async def test_bad_budget_echo_retains_ack_and_deletes_owned_session(
    claimed_openai_worker, monkeypatch, stage, echo
):
    from daimon.core.errors import TurnError
    from daimon.core.fleet import spawn_operation_key
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import fleet, mux_state
    from mux.errors import ContinuityLost

    request, root, worker, prices, _, calls, wire = claimed_openai_worker
    key = spawn_operation_key(worker.run_id, worker.ordinal) + ":session_create:0"
    cleanup_receipts = []
    original = httpx.MockTransport.handle_async_request

    async def echo_and_inspect(transport, call):
        if call.method == "DELETE":
            async with request.deps.sessionmaker() as db:
                receipt = await mux_state.get_operation(db, request.scope, key)
            assert receipt is not None and receipt.operation.status == "processed"
            assert receipt.result["ref"]["id"] == "native-openai-session"
            assert receipt.result["state"] == "provisioning"
            cleanup_receipts.append(receipt)
        response = await original(transport, call)
        selected = (
            call.method == "POST" and call.url.path == "/v1/agents/sessions"
            if stage == "ack"
            else call.method == "GET" and call.url.path.endswith("native-openai-session")
        )
        if selected and response.status_code == 200:
            raw = json.loads(response.content)
            if echo is None:
                raw.pop("spend_control", None)
            else:
                raw["spend_control"] = {"limit": echo}
            return httpx.Response(200, json=raw)
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", echo_and_inspect)
    with pytest.raises((ContinuityLost, TurnError, AdmissionDenied)):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="Refused budget",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert wire.deleted and len(cleanup_receipts) == 1
    restarted_store = mux_state.PostgresStateStore(request.deps.sessionmaker)
    receipt = await restarted_store.get_operation(request.scope, key)
    assert receipt is not None and receipt.result["ref"]["id"] == "native-openai-session"
    deletion = await restarted_store.get_operation(
        request.scope, spawn_operation_key(worker.run_id, worker.ordinal) + ":session_delete:0"
    )
    assert deletion is not None and deletion.operation.status == "processed"
    assert deletion.result["deleted"][0]["id"] == "native-openai-session"
    restarted = replace(request, deps=replace(request.deps, state_store=restarted_store))
    with pytest.raises((ContinuityLost, TurnError, AdmissionDenied)):
        await run_worker(
            restarted,
            parent=root,
            worker=worker,
            trigger_message="No reallocation",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert (
        sum(call.method == "POST" and call.url.path == "/v1/agents/sessions" for call in calls) == 1
    )
    assert sum(call.method == "DELETE" for call in calls) == 1
    assert not any(call.method == "POST" and call.url.path.endswith("/events") for call in calls)
    async with request.deps.sessionmaker.begin() as db:
        _, rows = await fleet.run_status(db, tenant_id=request.tenant_id, run_id=worker.run_id)
    retained = next(row for row in rows if row.id == worker.id)
    assert retained.turns_used == 0 and retained.binding_id is None
    assert retained.reserved_usd == worker.budget_usd


@pytest.mark.parametrize("native_budget", [Decimal("0.01")])
async def test_budgeted_session_without_ack_remains_unknown(claimed_openai_worker, monkeypatch):
    from daimon.core.fleet import spawn_operation_key
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import mux_state

    request, root, worker, prices, _, calls, wire = claimed_openai_worker
    original = httpx.MockTransport.handle_async_request

    async def lost_ack(transport, call):
        response = await original(transport, call)
        if call.method == "POST" and call.url.path == "/v1/agents/sessions":
            raise httpx.ReadError("offline no session ACK", request=call)
        return response

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", lost_ack)
    with pytest.raises(ProviderError):
        await run_worker(
            request,
            parent=root,
            worker=worker,
            trigger_message="No ACK",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert not wire.deleted
    key = spawn_operation_key(worker.run_id, worker.ordinal) + ":session_create:0"
    receipt = await mux_state.PostgresStateStore(request.deps.sessionmaker).get_operation(
        request.scope, key
    )
    assert receipt is not None and receipt.operation.status == "outcome_unknown"
    assert receipt.operation.resource is None and not receipt.result
    assert (
        sum(call.method == "POST" and call.url.path == "/v1/agents/sessions" for call in calls) == 1
    )
    assert not any(
        call.method == "DELETE" or call.method == "POST" and call.url.path.endswith("/events")
        for call in calls
    )


def _astra(request):
    from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default

    revision = request.admission.backend_revision
    astra = ConfigRevision.create(
        revision.channel,
        revision.local,
        resolve_default(
            BackendConfig(backend="openai", profile=revision.profile, model="gpt-6-astra")
        ),
    )
    return replace(request, admission=replace(request.admission, backend_revision=astra))


def _with_astra(plan, owned, monkeypatch, *, spend_limit: int | None = None) -> None:
    # The deployment row authorizes Astra alongside Luna, as a fleet row would.
    grant = plan.configuration_grant
    assert grant is not None
    widened = plan.model_copy(
        update={
            "configuration_grant": grant.model_copy(
                update={"models": (*grant.models, "gpt-6-astra")}
            ),
            "spend_limit_usd_cents": spend_limit,
        }
    )
    manifest(monkeypatch, owned.manifest_path, [widened.model_dump(mode="json")])


async def test_astra_worker_runtime_runs_at_astra_controls_and_price(
    target: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, plan, _, owned = target
    _with_astra(plan, owned, monkeypatch, spend_limit=5)
    # ROOT: on our project (no native session cap) Astra runs at its own price, bounded
    # by admission and the daily model cap; the row's limit is never sent.
    runtime = build_openai_runtime(_astra(request))
    assert runtime.controls.model == "gpt-6-astra"
    assert runtime.price is not None and runtime.price.model == "gpt-6-astra"
    assert (runtime.price.input, runtime.price.output) == (Decimal("10.00"), Decimal("50.00"))
    assert runtime.controls.spend_limit_usd_cents is None
    # Where a project honours native caps, the row's limit becomes `spend_control`.
    openai_native_spend_cap(monkeypatch)
    assert build_openai_runtime(_astra(request)).controls.spend_limit_usd_cents == 5


async def test_astra_runs_without_a_native_spend_limit(
    target: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ROOT: Astra runs under the host guard and the daily model cap, not a native cap."""
    request, _, plan, _, owned = target
    _with_astra(plan, owned, monkeypatch)
    assert request.worker.limits.native_budget_usd is None and plan.spend_limit_usd_cents is None
    assert build_openai_runtime(_astra(request)).controls.spend_limit_usd_cents is None
    assert build_openai_runtime(request).controls.spend_limit_usd_cents is None
