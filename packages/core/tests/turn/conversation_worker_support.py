"""Real conversation-only worker fixture shared by provider and fleet regressions."""

import json
import re
from contextlib import aclosing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import mux.drivers.openai as deployment
import pytest
from daimon.core.authz import Subject
from daimon.core.channel_backend import AgentBackendService, BackendActor
from daimon.core.fleet_orchestration import HostFleetPorts
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.neutral_resources import AgentFleetGrant, WorkerLimits, resolve_worker_spec
from daimon.core.pricing import provider_prices
from daimon.core.stores import fleet, tenant_ledger, tenants
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.core.turn.outcomes import drain_outcomes
from daimon.core.turn.runtime_manifest import capture_preparation_source, read_frozen_root
from daimon.core.turn.runtimes import build_channel_runtime
from mux.contracts.resources import AgentPatch, AgentSpec
from mux.drivers.openai.transport import SDKTransport
from mux.testing.isolated_sdk import build_async_openai

from .native_counterpart_support import db_preflight as db_preflight
from .native_counterpart_support import owned_database_preflight as owned_database_preflight
from .test_frozen_preparation import default_source_turn
from .test_frozen_root import freeze, root_turn
from .test_openai_host import SESSION_ID, Wire, openai_native_spend_cap

PROFILE = "openai.conversation_only"
DROPS = ("mcp", "memory", "sandbox", "skills", "tools")


class ConversationWire(Wire):
    def __init__(self):
        super().__init__()
        self.expected_spend_limit = 1
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
            if self.fail_create:
                raise httpx.ReadError("lost agent-create acknowledgement", request=call)
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
            assert body["spend_control"] == {"limit": 1}
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
        if (
            path.endswith("/events")
            and call.method == "POST"
            and json.loads(call.content)["events"][0]["type"] != "agent.session.input.cancel"
        ):
            raise AssertionError("initial input must never be sent after creation")
        result = super().handle(call)
        if path.endswith("/events") and call.method == "GET":
            self.sent.set()
        return result


@pytest.fixture
async def conversation_root(
    db_preflight, db_clean, db_committing_engine, monkeypatch, tmp_path, request
):
    # These workers carry a native budget: exercise a project that honours it.
    openai_native_spend_cap(monkeypatch)
    async with aclosing(
        root_turn.__wrapped__(
            db_preflight,
            db_committing_engine,
            live_root=True,
            deadline_seconds=600 if getattr(request, "param", None) == "load" else 120,
        )
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
        if getattr(request, "param", None) == "load":
            # Main's three-fleet load scenario grants ten minutes to the run
            # and its workers; the recorded single-worker fixture uses two.
            plan["grant"]["max_worker_seconds"] = 600
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
                        deadline=min(
                            parent.limits.deadline,
                            datetime.now(UTC) + timedelta(seconds=value.max_worker_seconds),
                        ),
                        max_turns=1,
                        native_budget_usd=Decimal("0.01"),
                    ),
                    captured_source=captured,
                )
                yield deps, parent, policy, resolved, wire, ports, authorized, transport
            finally:
                await ports.close()
                await drain_outcomes()


@pytest.fixture(params=(False, True), ids=("fresh", "clone"))
async def worker_root(conversation_root, request):
    # Generic orchestration uses a supported unattended profile. Hosted
    # automatic-tool rejection stays covered by the dedicated worker-plan tests.
    deps, root, policy, resolved, wire, ports, _, transport = conversation_root
    original = await ports._prepare_original(root)
    source = original.admission.agent.model_dump(mode="json")
    # Drift tests must mutate the response returned by the real source read,
    # rather than a detached copy of the admission snapshot.
    source_path = "/v1/agents/" + original.admission.agent.id
    transport.router.routes[:] = [
        route
        for route in transport.router.routes
        if not (route[0] == "GET" and route[1].fullmatch(source_path))
    ]
    transport.router.add(
        "GET", re.escape(source_path), lambda call, match: httpx.Response(200, json=source)
    )
    captured = await capture_preparation_source(original)
    assert captured.source is not None
    resolved = resolve_worker_spec(
        root,
        captured.source if request.param else resolved.spec,
        overrides=AgentPatch.model_validate(resolved.spec.model_dump(mode="json"))
        if request.param
        else None,
        policy=policy,
        now=original.now(),
        backend=resolved.backend,
        limits=resolved.limits,
        resources=(),
        environment=None,
        captured_source=captured,
    )
    async with deps.sessionmaker.begin() as db:
        await fleet.add_worker(
            db,
            tenant_id=original.tenant_id,
            run_id=root.fleet.run_id,
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
    pool = fleet.WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=2)
    row = await pool.claim(
        deps.sessionmaker, tenant_id=original.tenant_id, run_id=root.fleet.run_id
    )
    assert row is not None
    transport.requests.clear()
    try:
        yield original, root, row, resolved, ports.prices, wire.requests, wire, transport, source
    finally:
        pool.release()


@pytest.fixture
async def safe_claimed_openai_worker(worker_root, native_budget):
    request, root, row, resolved, prices, calls, wire, _, _ = worker_root
    selected = await request.for_worker(root, resolved)
    if native_budget is not None:
        assert selected.worker.limits.native_budget_usd == native_budget
    return selected, root, row, prices, None, calls, wire
