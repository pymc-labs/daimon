"""Warm native session receipts through the real public session caller."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from anthropic import APIConnectionError
from daimon.core.authz import Surface
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.neutral_resources import resolve_worker_spec
from daimon.core.scope import DeploymentDefault, ResolvedConfig
from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
from daimon.core.stores import access_policy, fleet, mux_state, tenant_ledger, tenants
from daimon.core.turn.admission import Admission, AdmissionGrant
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.testing.ma import MARouter, list_response, send_events_response, sse_response
from daimon.testing.ma_models import ma_agent, ma_session
from daimon.testing.ma_transport import ScriptedTransport
from mux.contracts.config import ConfigRevision
from mux.contracts.resources import ProviderBinding

from ..test_fleet_budget import tariffs
from ..test_fleet_permissions import POLICY
from ..test_sessions import worker_source_context
from .conftest import make_agent_message, make_status_idle

NOW = datetime(2026, 10, 12, 18, tzinfo=UTC)


@pytest.fixture(params=[True])
def source_read_only(request):
    return request.param


@pytest.fixture
async def native_worker(db_preflight, db_nullpool_engine, db_clean, source_read_only):
    sm, tenant, account, source, env, old, captured = await worker_source_context(
        db_nullpool_engine, warm=True
    )
    root = replace(
        old.permissions,
        fleet=replace(old.permissions.fleet, depth=0),
        agent_id=captured.agent_id,
        environment=captured.environment.spec,
        limits=old.limits.model_copy(
            update={"deadline": datetime.now(UTC) + timedelta(minutes=10)}
        ),
    )
    async with sm.begin() as db:
        await access_policy.set_access_policy(db, tenant_id=tenant, policy=POLICY)
        await tenants.set_funding_mode(db, tenant_id=tenant, funding_mode="prepaid")
        await tenant_ledger.insert_entry(
            db,
            tenant_id=tenant,
            delta_usd=Decimal("10"),
            reason="trial_credit",
            idempotency_key="native-worker-funds",
        )
        revision = ConfigRevision.create(root.fleet.root.channel, 1, root.backend_grants[0])
        await mux_state.put_config_revision(db, revision, origin="agent")
        await mux_state.put_binding(
            db,
            ProviderBinding(
                id="root-binding",
                thread=root.fleet.root,
                legacy_account_id=str(account),
                provider="anthropic",
                profile=revision.profile,
                native_refs={"session": "root-session"},
                generation=1,
                config_revision=1,
            ),
            expected_generation=0,
        )
        run = await fleet.create_run(
            db,
            tenant_id=tenant,
            orchestrator_account_id=account,
            orchestrator_session_id="root-session",
            orchestrator_binding_id="root-binding",
            platform="discord",
            channel_id=root.fleet.root.channel.channel_id,
            thread_id=root.fleet.root.thread_id,
            operation_key="native-worker-run",
            total_budget_usd=root.limits.budget_usd,
            deadline_at=root.limits.deadline,
            max_workers=1,
            created_by="host",
        )
    root = replace(root, fleet=replace(root.fleet, run_id=run.id))
    from daimon.core.sessions import project_session_resources

    captured = await project_session_resources(
        captured,
        agent=source,
        environment=env,
        session_factory=sm,
        tenant_id=tenant,
        agent_uuid=captured.agent_id,
        account_id=account,
        resource_scope=root.fleet.scope,
        memory_read_only=source_read_only,
    )
    resolved = resolve_worker_spec(
        root,
        root.spec,
        policy=POLICY,
        now=datetime.now(UTC),
        resources=root.resources,
        environment=root.environment,
        captured_source=captured,
    )
    async with sm.begin() as db:
        await fleet.add_worker(
            db,
            tenant_id=tenant,
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
    worker = await pool.claim(sm, tenant_id=tenant, run_id=run.id)
    assert worker is not None
    router = MARouter()
    native = {}
    controls = {"fault": None}

    def frozen_agent(raw):
        fault = controls["fault"]
        if fault == "frozen_model":
            raw["agent"]["model"]["id"] = "claude-opus-4-6"
        elif fault == "frozen_system":
            raw["agent"]["system"] = "unapproved instructions"
        elif fault == "frozen_version":
            raw["agent"]["version"] = 99
        elif fault == "frozen_tools":
            raw["agent"]["tools"] = [
                {
                    "type": "agent_toolset_20260401",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            ]
        elif fault == "frozen_mcp":
            raw["agent"]["mcp_servers"] = [
                {"type": "url", "name": "unapproved", "url": "https://unapproved.invalid/mcp"}
            ]
        elif fault == "foreign_session":
            raw["metadata"]["daimon_account"] = "foreign-account"
        elif fault == "frozen_skills":
            raw["agent"]["skills"] = [{"type": "custom", "skill_id": "unapproved", "version": "1"}]
        return raw

    def resources(call, match):
        if call.method == "POST":
            payload = json.loads(call.content)
            if call.url.path == "/v1/agents":
                native["agent"] = ma_agent(
                    id="new-worker",
                    tenant_id=str(tenant),
                    name=payload["name"],
                    model=payload["model"],
                    system=payload.get("system"),
                    metadata=payload.get("metadata"),
                    tools=payload.get("tools") or (),
                    skills=payload.get("skills") or (),
                )
                native["agent"] = native["agent"].model_copy(
                    update={"metadata": payload.get("metadata")}
                )
                if controls["fault"] == "lost_ack":
                    raise httpx.ReadError("lost native ACK", request=call)
                return httpx.Response(200, json=native["agent"].model_dump(mode="json"))
            native["session_posts"] = native.get("session_posts", 0) + 1
            native["session"] = ma_session(
                id="worker-session",
                agent=native["agent"],
                environment_id=payload["environment_id"],
                metadata=payload["metadata"],
                resources=payload["resources"],
            )
            if controls["fault"] == "lost_session_ack" and native["session_posts"] == 1:
                raise httpx.ReadError("lost session ACK", request=call)
            return httpx.Response(200, json=frozen_agent(native["session"].model_dump(mode="json")))
        if "/agents/" in call.url.path:
            agent = source if call.url.path.endswith(source.id) else native["agent"]
            raw = agent.model_dump(mode="json", exclude_unset=True)
            if (
                controls["fault"] == "source_drift"
                and agent.id == source.id
                and "session" in native
            ):
                raw["system"] = "changed source instructions"
            if controls["fault"] == "agent_archived" and agent.id == "new-worker":
                raw["archived_at"] = NOW.isoformat()
            if controls["fault"] == "agent_drift" and agent.id == "new-worker":
                raw["system"] = "changed instructions"
            return httpx.Response(200, json=raw)
        if "/environments/" in call.url.path:
            return httpx.Response(200, json=env.model_dump(mode="json", exclude_unset=True))
        if controls["fault"] == "interrupted_proof":
            controls["fault"] = None
            raise asyncio.CancelledError("proof interrupted after session ACK")
        raw = frozen_agent(native["session"].model_dump(mode="json"))
        if controls["fault"] == "frozen_get_only":
            raw["agent"]["system"] = "unapproved readback instructions"
        if controls["fault"] == "resource_drift":
            raw["resources"][0]["memory_store_id"] = "foreign-memory"
        return httpx.Response(200, json=raw)

    from daimon.testing.ma import FakeMemoryStoreState, make_fake_memory_store_handler

    memory = make_fake_memory_store_handler(FakeMemoryStoreState())
    router.add("POST", r"/v1/memory_stores", lambda call, match: memory(call))
    router.add("POST", r"/v1/agents", resources)
    router.add("POST", r"/v1/sessions", resources)
    router.add("GET", r"/v1/agents/.*", resources)
    router.add("GET", r"/v1/environments/.*", resources)
    router.add("GET", r"/v1/sessions/worker-session", resources)

    def archive(call, match):
        native["session"] = native["session"].model_copy(update={"archived_at": NOW})
        return httpx.Response(200, json=native["session"].model_dump(mode="json"))

    router.add("POST", r"/v1/sessions/worker-session/archive", archive)
    router.add(
        "POST", r"/v1/sessions/worker-session/events", lambda call, match: send_events_response()
    )
    router.add("GET", r"/v1/sessions/worker-session/events", lambda call, match: list_response([]))
    router.add(
        "GET",
        r"/v1/sessions/worker-session/events/stream",
        lambda call, match: sse_response(
            [
                make_agent_message(
                    event_id="worker-output", text="verified-worker-result"
                ).model_dump(mode="json"),
                make_status_idle(event_id="worker-idle").model_dump(mode="json"),
            ]
        ),
    )
    trace = ScriptedTransport(router=router)
    async with trace.client() as base_client:
        client = base_client.with_options(max_retries=8)
        deps = TurnDeps(
            client,
            sm,
            DeploymentDefault(),
            new_resolver_cache(),
            Path("/nonexistent"),
            McpSettings(),
            None,
            Decimal(1),
            None,
            None,
            None,
            None,
            None,
            turn_path="mux",
            channel_backends=True,
            agent_backends=True,
            state_store=mux_state.PostgresStateStore(sm),
        )
        grant = AdmissionGrant(
            tenant_id=tenant,
            subject=root.subject,
            surface=Surface.CHANNEL,
            turn_place=root.fleet.place,
            agent=root.agent,
            run_place=root.fleet.place,
            channel_id=root.fleet.root.channel.channel_id,
            thread_id=root.fleet.root.thread_id,
            is_dm=False,
        )
        admission = Admission(
            account_id=account,
            agent=source,
            environment=env,
            config=ResolvedConfig(agent_name="source", environment_name="default"),
            grant=grant,
            memory_read_only=source_read_only,
            backend_revision=revision,
            backend_revision_origin="agent",
        )
        request = ProviderPreparationRequest(
            deps,
            admission,
            resolved.permissions.fleet.scope,
            tenant,
            "discord",
            root.subject.platform_user_id,
            "worker-thread",
            account,
            True,
            DEFAULT_MA_CAPABILITIES,
            None,
            resolved.limits.deadline,
            lambda: datetime.now(UTC),
            worker=resolved,
        )
        try:
            yield request, root, worker, trace, controls
        finally:
            pool.release()


@pytest.mark.parametrize("fault", [None, "lost_session_ack", "interrupted_proof"])
async def test_session_receipt_public_caller_uses_real_scope_guard_and_sdk(native_worker, fault):
    from daimon.core.channel_budget import admit_fleet_worker, require_fleet_worker_admission
    from daimon.core.fleet import spawn_operation_key, worker_slot
    from daimon.core.mux_backend import managed_agents
    from daimon.core.mux_compat import sdk_agent
    from daimon.core.turn.persistence import TurnPersistence, UncertainSend
    from daimon.core.turn.prepare import create_ma_session
    from daimon.core.turn.runtime_manifest import capture_preparation_source
    from mux.errors import ProviderError

    request, root, claimed, trace, controls = native_worker
    resolved = request.worker
    assert resolved is not None
    async with request.deps.sessionmaker.begin() as db:
        usage = await admit_fleet_worker(
            db,
            parent=root,
            policy=POLICY,
            resolved=resolved,
            ordinal=claimed.ordinal,
            role=claimed.role,
            prices=tariffs(root),
            markup=request.deps.markup,
        )
    assert usage.admitted and usage.worker.id == claimed.id
    source = await capture_preparation_source(request)
    backend = managed_agents(request.deps.anthropic, scope=request.scope)
    agent = await backend.agents.create(request.scope, resolved.spec, key="owned-test-agent")
    executing = replace(
        request.admission,
        agent=sdk_agent(agent),
        memory_read_only=True,
        budget_channel_id=root.fleet.root.channel.channel_id,
        origin_channel_id=root.fleet.root.channel.channel_id,
        origin_thread_id=worker_slot(usage.run, claimed).thread.thread_id,
    )

    async def fresh_guard():
        async with request.deps.sessionmaker.begin() as db:
            policy = await access_policy.load_access_policy(db, tenant_id=request.tenant_id)
            verdict = await require_fleet_worker_admission(
                db,
                parent=root,
                policy=policy,
                worker_id=claimed.id,
                prices=tariffs(root),
                markup=request.deps.markup,
            )
        assert verdict.admitted

    selected = replace(request, admission=executing, source=source, before_native=fresh_guard)
    persistence = TurnPersistence(
        request.deps.state_store,
        None,
        request.scope,
        slot=worker_slot(usage.run, claimed),
        operation_key=spawn_operation_key(claimed.run_id, claimed.ordinal),
        now=request.now,
    )

    async def prepare():
        return await create_ma_session(
            request.deps, executing, tenant_id=request.tenant_id, preparation=selected
        )

    controls["fault"] = fault
    if fault is not None:
        with (
            persistence.activate(),
            pytest.raises(
                (asyncio.CancelledError,)
                if fault == "interrupted_proof"
                else (ProviderError, UncertainSend, APIConnectionError)
            ),
        ):
            await persistence.run(prepare)
    if fault == "lost_session_ack":
        with persistence.activate(), pytest.raises(UncertainSend):
            await persistence.run(prepare)
    else:
        with persistence.activate():
            created = await persistence.run(prepare)
        assert created.ma_session_id == "worker-session"
        assert created.snapshot.model_id == resolved.spec.model.id
    receipt = await request.deps.state_store.get_operation(
        request.scope, spawn_operation_key(claimed.run_id, claimed.ordinal) + ":session_create:0"
    )
    assert receipt is not None
    assert receipt.operation.status == (
        "outcome_unknown" if fault == "lost_session_ack" else "processed"
    )
    assert (
        sum(call.method == "POST" and call.path == "/v1/sessions" for call in trace.requests) == 1
    )
    assert not any(call.path.endswith("/events") for call in trace.requests)
