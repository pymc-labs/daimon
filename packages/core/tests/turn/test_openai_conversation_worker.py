"""Recorded MA root reaches a counted conversation-only worker through the installed host."""

import asyncio
import json
from contextlib import aclosing
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import mux.drivers.openai as deployment
import pytest
from daimon.core.authz import Subject
from daimon.core.channel_backend import AgentBackendService, BackendActor
from daimon.core.fleet import spawn_operation_key, worker_slot
from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.neutral_resources import AgentFleetGrant, WorkerLimits, resolve_worker_spec
from daimon.core.pricing import provider_prices
from daimon.core.stores import fleet, tenant_ledger, tenants
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.runtime_manifest import read_frozen_root
from daimon.core.turn.runtimes import build_channel_runtime
from mux.contracts.resources import AgentSpec
from mux.drivers.openai.transport import SDKTransport
from mux.testing.isolated_sdk import build_async_openai

from .test_fleet_native_dispatch import _settled
from .test_frozen_preparation import default_source_turn
from .test_frozen_root import freeze, root_turn
from .test_openai_host import SESSION_ID, Wire
from .test_openai_worker_plan import db_preflight as db_preflight
from .test_openai_worker_plan import owned_database_preflight as owned_database_preflight

PROFILE = "openai.conversation_only"
DROPS = ("mcp", "memory", "sandbox", "skills", "tools")


class ConversationWire(Wire):
    def __init__(self):
        super().__init__()
        self.expected_spend_limit = None
        self.agent = {}
        self.posts = []
        self.lose_ack = False
        self.error = None

    def session(self):
        return {
            **super().session(),
            "environment": {"type": "none"},
            "metadata": {"mux_tenant": self.agent["metadata"]["mux_tenant"]},
        }

    def handle(self, call):
        path = call.url.path.removeprefix("/v1")
        if call.method == "POST":
            self.posts.append(call)
        assert "environment" not in path and "skills" not in path and "vault" not in path
        if path == "/agents" and call.method == "POST":
            self.requests.append(call)
            self.agent = {**json.loads(call.content), "id": "native-agent", "created_at": 0}
            assert self.agent["tools"] == []
            return httpx.Response(200, json=self.agent)
        if path == "/agents/native-agent":
            self.requests.append(call)
            return httpx.Response(200, json=self.agent)
        if path == "/agents/sessions" and call.method == "POST":
            self.requests.append(call)
            body = json.loads(call.content)
            assert body["environment"] == {"type": "none"}
            # Our project rejects `spend_control`: a Luna worker's budget stays host-side.
            assert "spend_control" not in body
            assert body["agent"] == {"model": "gpt-6-luna", "multi_agent": {"enabled": False}}
            assert "vault_ids" not in body
            text = body["input"][0]["content"][0]["text"]
            # Exercise the same event/usage fixtures as the persistent host,
            # with input accepted by create rather than a native events POST.
            internal = httpx.Request(
                "POST",
                "https://openai.invalid/v1/agents/sessions/" + SESSION_ID + "/events",
                headers={"OpenAI-Beta": "agents=v1", "Idempotency-Key": "fixture:send"},
                json={"events": [{"type": "agent.session.input", "input": body["input"]}]},
            )
            Wire.handle(self, internal)
            self.requests.remove(internal)
            assert text in self.items[0]["content"][0]["text"]
            if self.lose_ack:
                raise httpx.ReadError("lost session-create acknowledgement", request=call)
            return httpx.Response(200, json=self.session())
        if path.endswith("/events") and call.method == "POST":
            raise AssertionError("initial input must never be sent after creation")
        result = super().handle(call)
        if path.endswith("/events") and call.method == "GET":
            self.sent.set()
        return result


@pytest.fixture
async def conversation_root(
    db_preflight, db_clean, db_committing_engine, monkeypatch, tmp_path, request
):
    async with aclosing(
        root_turn.__wrapped__(db_preflight, db_committing_engine, live_root=True)
    ) as roots:
        updated, current, source = await default_source_turn(await anext(roots))
        deps, prepared, _, intent, _, scope, _, transport = updated
        if getattr(request, "param", None) == "memory_read_only":
            admission = replace(prepared.admission, memory_read_only=True)
            prepared = replace(
                prepared,
                admission=admission,
                _preparation=replace(prepared._preparation, admission=admission),
            )
        deps = replace(
            deps,
            agent_backends=True,
            channel_backends=True,
            turn_runtime_factory=build_channel_runtime,
        )
        plan = json.loads(
            (
                Path(__file__).resolve().parents[1] / "fixtures/i7_r6_planned_fleet_grant.json"
            ).read_text()
        )
        plan["grant"]["limits"]["deadline"] = current.expires_at.isoformat()
        for backend, _ in plan["grant"]["choices"]:
            if backend["backend"] == "openai":
                backend["profile"] = PROFILE
        if getattr(request, "param", None) != "recorded_caps":
            # Attenuate only an unused sibling ceiling: the recorded ceilings
            # total .585, which the existing .49 run-cap invariant refuses.
            plan["grant"]["limits"]["provider_limits_usd"]["gemini"] = "0.395"
        value = AgentFleetGrant.model_validate_json(json.dumps(plan["grant"]))
        service = AgentBackendService(deps.sessionmaker, enabled=True)
        actor = BackendActor(
            source.tenant_id, prepared.admission.account_id, Subject(is_admin=True)
        )
        if getattr(request, "param", None) == "recorded_caps":
            from daimon.core.channel_backend import AgentBackendRefused

            # Financial admission would refuse every spawn; the grant never lands.
            with pytest.raises(AgentBackendRefused, match="fleet provider limits exceed budget"):
                await service.preview_fleet_grant(source, value, now=datetime.now(UTC))
            current_grant = await service.current(source)
            assert current_grant is None or "provider_limits_usd" not in (
                (current_grant.fleet_grant or {}).get("limits", {})
            )
            yield None
            return
        preview = await service.preview_fleet_grant(source, value, now=datetime.now(UTC))
        await service.set_fleet_grant(
            source,
            value,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
            now=datetime.now(UTC),
        )
        async with deps.sessionmaker.begin() as db:
            await tenants.set_funding_mode(db, tenant_id=source.tenant_id, funding_mode="prepaid")
            await tenant_ledger.insert_entry(
                db,
                tenant_id=source.tenant_id,
                delta_usd=Decimal("10"),
                reason="trial_credit",
                idempotency_key=f"worker-credit:{source.tenant_id}",
            )
        await freeze((deps, prepared, *updated[2:]))
        parent, policy = await read_frozen_root(
            deps, scope, intent_id=intent.id, now=datetime.now(UTC)
        )
        assert len(parent.spec.tools) == 8
        assert parent.environment is not None
        assert (
            parent.resources
        )  # Captured root includes memory; the worker must explicitly drop it.
        backend, model = next(pair for pair in value.choices if pair[0].backend == "openai")
        spec = parent.spec.model_copy(
            update={
                "model": model,
                "tools": (),
                "skills": (),
                "mcp_servers": (),
                "extensions": None,
                "declared_drops": DROPS,
            }
        )
        if getattr(request, "param", None) == "fresh_spec":
            # Fresh tool arguments omit optional workspace fields and ownership stamps.
            spec = AgentSpec(
                name="W2",
                model=model,
                system="Reject B: clones only narrow.",
                declared_drops=("mcp", "memory", "model_controls", "sandbox", "skills", "tools"),
            )
        wire = ConversationWire()
        manifest = tmp_path / "runtime.json"
        authorized = OpenAIDeployment(
            profile=PROFILE,
            tenant_id=scope.tenant_id,
            platform=parent.fleet.root.channel.platform,
            channel_id=parent.fleet.root.channel.channel_id,
            account_id=scope.account_id,
            project="project",
            api_key_env="CONVERSATION_OFFLINE_KEY",
            worker_materialization=True,
            source_agent_id=derive_agent_uuid(
                tenant_id=source.tenant_id, ma_agent_id=source.source.ref.id
            ),
            source_agent_version=source.source.revision.local,
            configuration_grant={
                "profiles": [PROFILE],
                "models": [model.id],
                "thread_modes": [backend.thread_mode],
                "requires": dict(backend.requires),
            },
        )
        manifest.write_text(json.dumps([authorized.model_dump(mode="json")]))
        monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
        monkeypatch.setenv(authorized.api_key_env, "offline")

        def transport_call(call):
            try:
                return wire.handle(call)
            except BaseException as error:
                wire.error = error
                raise

        async with build_async_openai(
            transport=httpx.MockTransport(transport_call),
            api_key="offline",
            base_url="https://openai.invalid/v1",
        ) as sdk:
            monkeypatch.setattr(
                deployment, "configured_transport", lambda **kwargs: SDKTransport(sdk)
            )
            ports = HostFleetPorts(
                deps.sessionmaker,
                prices=provider_prices(PROFILE),
                markup=Decimal("1"),
                deps=deps,
                pool=fleet.WorkerPool(
                    max_concurrent_workers=1, max_concurrent_workers_per_tenant=1
                ),
            )
            try:
                captured = await ports.capture_source(parent)
                resolved = resolve_worker_spec(
                    parent,
                    spec,
                    policy=policy,
                    now=datetime.now(UTC),
                    backend=backend,
                    environment=None,
                    resources=(),
                    limits=WorkerLimits(
                        budget_usd=Decimal("0.055"),
                        deadline=parent.limits.deadline,
                        max_turns=1,
                        native_budget_usd=Decimal("0.01"),
                    ),
                    captured_source=captured,
                )
                yield deps, parent, policy, resolved, wire, ports, authorized, transport
            finally:
                await ports.close()
                await drain_outcomes()


def terms(parent, resolved):
    return dict(
        now=datetime.now(UTC),
        run_id=parent.fleet.run_id,
        operation_key="conversation-worker",
        role="worker",
        backend=resolved.backend,
        agent=resolved.spec,
        limits=resolved.limits,
    )


@pytest.mark.parametrize("conversation_root", [None, "fresh_spec"], indirect=True)
async def test_recorded_root_freeze_retained_reader_dispatch_create_events_usage(conversation_root):
    deps, parent, policy, resolved, wire, ports, _, transport = conversation_root
    owner = FleetOrchestrator(ports)
    before = len(transport.requests)
    kwargs = terms(parent, resolved)
    first = await owner.spawn(parent, policy, **kwargs)
    second = await owner.spawn(parent, policy, **kwargs)
    assert first["worker_id"] == second["worker_id"]
    task = ports._tasks.get(UUID(first["worker_id"]))
    if task is not None:
        try:
            await task
        except BaseException:
            if wire.error is not None:
                raise wire.error from None
            raise
    worker = await _settled(ports, parent, first["worker_id"])
    assert worker.status == "succeeded" and worker.turns_used == 1
    assert worker.result.excerpt == "answer turn-1"
    assert [call.url.path for call in wire.posts] == ["/v1/agents", "/v1/agents/sessions"]
    assert (
        "Complete the task in your instructions."
        in json.loads(wire.posts[-1].content)["input"][0]["content"][0]["text"]
    )
    assert all(call.method == "GET" for call in transport.requests[before:])
    binding = await deps.state_store.get_binding(
        worker_slot((await ports.read_run(parent.fleet.scope, parent.fleet.run_id))[0], worker)
    )
    assert binding.profile == PROFILE
    from daimon.core.stores.turn_usage import read_accounting_revisions

    async with deps.sessionmaker() as db:
        usage = await read_accounting_revisions(db, tenant_id=worker.tenant_id, binding=binding)
    assert usage.revisions
    assert usage.revisions[-1].observation.input_tokens == 100
    assert usage.revisions[-1].observation.output_tokens == 20
    operation = await deps.state_store.get_operation(
        parent.fleet.scope, spawn_operation_key(worker.run_id, worker.ordinal) + ":send:0"
    )
    assert operation.operation.status == "processed"
    events = await deps.state_store.read_events(SESSION_ID, after=-1)
    assert any(e.type == "session.turn_ended" for e in events)
    assert any(e.type == "agent.message" for e in events)
    again = await owner.spawn(parent, policy, **kwargs)
    assert again["worker_id"] == first["worker_id"] and again["status"] == "succeeded"
    assert len(wire.posts) == 2


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_worker_task_logs_original_failure_without_exception_payload(
    conversation_root, monkeypatch, failure
):
    from structlog.testing import capture_logs

    _, parent, policy, resolved, wire, ports, _, _ = conversation_root
    secret = "sentinel-private-provider-payload"

    async def refuse(*args, **kwargs):
        raise failure(secret)

    monkeypatch.setattr("daimon.core.headless_runner.run_worker", refuse)
    with capture_logs() as logs:
        first = await FleetOrchestrator(ports).spawn(parent, policy, **terms(parent, resolved))
        task = ports._tasks.get(UUID(first["worker_id"]))
        assert task is not None
        with pytest.raises(failure):
            await task
        await asyncio.sleep(0)
    failures = [row for row in logs if row["event"] == "fleet.worker_task_failed"]
    assert failures == [
        {
            "event": "fleet.worker_task_failed",
            "run_id": str(parent.fleet.run_id),
            "worker_id": first["worker_id"],
            "error_class": failure.__name__,
            "causes": [{"type": "RuntimeError" if failure is RuntimeError else "redacted"}],
            "log_level": "warning",
        }
    ]
    assert secret not in json.dumps(logs)
    assert wire.posts == []


async def test_missing_dispatch_host_runtime_refuses_before_native_allocation(
    conversation_root, monkeypatch
):
    from daimon.core.turn.errors import AdmissionDenied

    _, parent, policy, resolved, wire, ports, _, _ = conversation_root
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    first = await FleetOrchestrator(ports).spawn(parent, policy, **terms(parent, resolved))
    task = ports._tasks.get(UUID(first["worker_id"]))
    assert task is not None
    with pytest.raises(AdmissionDenied) as refused:
        await task
    assert refused.value.reason == "backend_unsupported"
    _, workers = await ports.read_run(parent.fleet.scope, parent.fleet.run_id)
    actual = next(row for row in workers if str(row.id) == first["worker_id"])
    assert actual.binding_id is None and actual.turns_used == actual.attempts == 0
    assert actual.reserved_usd > 0
    assert wire.posts == []


@pytest.mark.parametrize("conversation_root", ["recorded_caps"], indirect=True)
async def test_recorded_provider_ceilings_refuse_before_native_allocation(conversation_root):
    """The recorded lines total .585 over a .49 run: refused at the grant preview."""
    assert conversation_root is None


@pytest.mark.parametrize("fault", ["environment", "tools", "mcp", "skills", "mount", "drop"])
async def test_conversation_worker_rejects_residual_authority_before_native_read(
    conversation_root, fault
):
    from daimon.core.turn.errors import AdmissionDenied
    from mux.contracts.ids import SkillRef
    from mux.contracts.resources import EnvironmentSpec, MCPConnection, ToolSpec

    _, parent, _, resolved, wire, ports, plan, _ = conversation_root
    async with ports._load_preparation(parent) as root_request:
        request = await root_request.for_worker(parent, resolved)
    changed = resolved
    if fault == "environment":
        changed = replace(resolved, environment=EnvironmentSpec(name="unexpected"))
    elif fault == "mount":
        changed = replace(resolved, resources=parent.resources)
    else:
        values = {
            "tools": {"tools": (ToolSpec(name="web_search", kind="builtin"),)},
            "mcp": {"mcp_servers": (MCPConnection(name="unexpected", url="https://mcp.invalid"),)},
            "skills": {"skills": (SkillRef(id="unexpected", version="1"),)},
            "drop": {"declared_drops": tuple(d for d in DROPS if d != "sandbox")},
        }
        changed = replace(resolved, spec=resolved.spec.model_copy(update=values[fault]))
    with pytest.raises(AdmissionDenied):
        plan.worker_environment(request, changed)
    assert not wire.requests


async def test_lost_create_ack_retry_and_restart_keep_one_consumed_input(conversation_root):
    from daimon.core.turn.persistence import UncertainSend
    from mux.errors import ProviderError

    deps, parent, policy, resolved, wire, ports, _, _ = conversation_root
    wire.lose_ack = True
    owner = FleetOrchestrator(ports)
    kwargs = terms(parent, resolved)
    first = await owner.spawn(parent, policy, **kwargs)
    task = ports._tasks.get(UUID(first["worker_id"]))
    assert task is not None
    with pytest.raises((ProviderError, UncertainSend)):
        await task
    run, rows = await ports.read_run(parent.fleet.scope, parent.fleet.run_id)
    worker = next(w for w in rows if str(w.id) == first["worker_id"])
    assert worker.turns_used == 1 and worker.binding_id is None
    assert worker.reserved_usd == run.reserved_usd == Decimal("0.055")
    operation = await deps.state_store.get_operation(
        parent.fleet.scope, spawn_operation_key(run.id, worker.ordinal) + ":send:0"
    )
    assert operation.operation.status == "outcome_unknown"
    assert await deps.state_store.get_binding(worker_slot(run, worker)) is None
    assert len(wire.posts) == 2
    await ports.close()
    restarted = HostFleetPorts(
        deps.sessionmaker,
        prices=provider_prices(PROFILE),
        markup=Decimal(1),
        deps=deps,
        pool=fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=1),
    )
    try:
        again = await FleetOrchestrator(restarted).spawn(parent, policy, **kwargs)
        assert again["worker_id"] == first["worker_id"]
        assert len(wire.posts) == 2
    finally:
        await restarted.close()


async def test_restart_replays_created_input_and_rejects_changed_task(conversation_root):
    from daimon.core.turn.openai_host import driver
    from daimon.core.turn.openai_io import OpenAITurnIO
    from daimon.core.turn.openai_state import initial_input_consumed, invocation_baseline
    from daimon.core.turn.persistence import TurnPersistence
    from mux.contracts.resources import Session
    from mux.errors import ScopeViolation

    deps, parent, policy, resolved, wire, ports, _, _ = conversation_root
    first = await FleetOrchestrator(ports).spawn(parent, policy, **terms(parent, resolved))
    task = ports._tasks.get(UUID(first["worker_id"]))
    if task is not None:
        await task
    worker = await _settled(ports, parent, first["worker_id"])
    assert worker.status == "succeeded"
    run, _ = await ports.read_run(parent.fleet.scope, parent.fleet.run_id)
    binding = await deps.state_store.get_binding(worker_slot(run, worker))
    key = spawn_operation_key(run.id, worker.ordinal)
    operation = await deps.state_store.get_operation(parent.fleet.scope, key + ":send:0")
    native = Session.model_validate(dict(operation.result))
    async with ports._load_preparation(parent) as root_request:
        request = await root_request.for_worker(parent, resolved)

    async def guard():
        return None

    request = replace(request, before_native=guard)
    runtime = build_channel_runtime(request)

    from daimon.core.turn.openai_host import prepare_openai
    from mux.contracts.ids import Revision
    from mux.contracts.resources import SessionSpec

    async def retained_plan(selected):
        return SessionSpec(
            agent=native.ref.model_copy(update={"kind": "agent", "id": "native-agent"}),
            agent_revision=Revision(local=0),
            config_revision=selected.admission.backend_revision.local,
        )

    runtime = replace(runtime, session_plan=retained_plan)
    reused = await prepare_openai(
        replace(
            request,
            thread_id=binding.thread.thread_id,
            reuse_existing=True,
            deps=replace(request.deps, turn_runtimes={PROFILE: runtime}),
        )
    )
    assert reused.reused and reused.ma_session_id == SESSION_ID
    assert reused.backend.capabilities().profile_id == PROFILE
    message = json.loads(wire.posts[-1].content)["input"][0]["content"][0]["text"]
    for text in (message, message, "a different task"):
        active = TurnPersistence(
            deps.state_store, binding, request.scope, operation_key=f"turn:{SESSION_ID}:{key}"
        )
        backend = driver(runtime, request.admission.backend_revision, request.scope, binding)
        io = OpenAITurnIO(
            backend,
            request.scope,
            native.ref,
            persistence=active,
            baseline_loader=invocation_baseline,
            initial_input_loader=initial_input_consumed,
        )

        async def replay(active=active, io=io, text=text):
            with active.activate():
                await io.send(
                    [{"type": "user.message", "content": [{"type": "text", "text": text}]}]
                )
                assert await io.replay()

        if text == message:
            await active.run(replay)
        else:
            with pytest.raises(ScopeViolation, match="initial input differs"):
                await active.run(replay)
    assert len(wire.posts) == 2 and worker.turns_used == 1


@pytest.mark.parametrize("conversation_root", ["memory_read_only"], indirect=True)
async def test_declared_memory_loss_retains_read_only_root_authority(conversation_root):
    _, parent, policy, resolved, _, ports, _, _ = conversation_root
    async with ports._load_preparation(parent) as original:
        assert original.admission.memory_read_only
        derived = await original.for_worker(parent, resolved)
        assert derived.admission.memory_read_only and derived.worker.permissions.memory_read_only
    result = await FleetOrchestrator(ports).spawn(parent, policy, **terms(parent, resolved))
    task = ports._tasks.get(UUID(result["worker_id"]))
    if task is not None:
        await task
    worker = await _settled(ports, parent, result["worker_id"])
    assert worker.status == "succeeded" and worker.turns_used == 1


async def test_exhausted_input_cap_refuses_before_session_create(conversation_root, monkeypatch):
    from daimon.core._models import FleetWorker as WorkerRow
    from daimon.core.fleet import FleetConflict
    from mux.errors import ScopeViolation
    from sqlalchemy import update

    deps, parent, policy, resolved, wire, ports, _, _ = conversation_root
    original = deps.state_store.claim_send

    async def claim(scope, key, *, now, fence):
        if key.endswith(":send:0"):
            # A concurrent input consumed the one available turn before this
            # transaction's claim. The financial/input owners must refuse it.
            async with deps.sessionmaker.begin() as db:
                await db.execute(
                    update(WorkerRow)
                    .where(WorkerRow.run_id == parent.fleet.run_id)
                    .values(turns_used=1, version=WorkerRow.version + 1)
                )
        return await original(scope, key, now=now, fence=fence)

    monkeypatch.setattr(deps.state_store, "claim_send", claim)
    first = await FleetOrchestrator(ports).spawn(parent, policy, **terms(parent, resolved))
    task = ports._tasks.get(UUID(first["worker_id"]))
    assert task is not None
    with pytest.raises((FleetConflict, ScopeViolation, asyncio.CancelledError)):
        await task
    assert [call.url.path for call in wire.posts] == ["/v1/agents"]


def test_conversation_deployment_requires_explicit_profile_and_no_hosted_authority():
    from pydantic import ValidationError

    base = dict(
        tenant_id="tenant",
        platform="discord",
        channel_id="channel",
        account_id="account",
        project="project",
        api_key_env="CONVERSATION_OFFLINE_KEY",
        config_digest="digest",
        agent_id="agent",
    )
    assert OpenAIDeployment(profile=PROFILE, **base).environment_id is None
    with pytest.raises(ValidationError, match="require an environment"):
        OpenAIDeployment(profile="openai.persistent_workspace", **base)
    for field in ({"environment_id": "environment"}, {"skill_ids": ("skill",)}):
        with pytest.raises(ValidationError, match="cannot authorize"):
            OpenAIDeployment(profile=PROFILE, **base, **field)
    from daimon.core.pricing import provider_prices

    assert (
        provider_prices(PROFILE)[("openai", "gpt-6-luna")]
        is provider_prices("openai.persistent_workspace")[("openai", "gpt-6-luna")]
    )
