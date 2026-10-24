"""A fleet worker that runs AS another on-server agent, under the requester's permissions."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from daimon.core.access_policy import AgentRule
from daimon.core.authz import build_agent_ref
from daimon.core.channel_backend import load_backend_agent
from daimon.core.mux_compat import retrieve_agent
from daimon.core.neutral_resources import (
    AgentFleetGrant,
    WorkerSpecRefused,
    agent_worker_backend,
    agent_worker_patch,
    agent_worker_permissions,
    resolve_worker_spec,
    worker_agent_allowed,
    worker_agents_line,
)
from daimon.core.sessions import project_session_resources
from daimon.core.stores import agent_memory_stores
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.ids import ModelRef
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

from ..test_agent_backend import owned as owned
from .test_fleet_source_permissions import NOW, compose
from .test_fleet_source_permissions import captured as captured

RESEARCHER_SYSTEM = "You are the research agent. Cite sources."


async def capture_agent(
    owned,
    scope,
    *,
    ma_id,
    name,
    model="claude-haiku-5-5",
    tools=(),
    memory_store=True,
    cli=False,
):
    """Capture another on-server agent the way the root's source is captured."""
    _, identity, actor, sessions = owned
    native = ma_agent(
        id=ma_id,
        name=name,
        tenant_id=identity.tenant_id,
        model=model,
        system=RESEARCHER_SYSTEM,
        version=1,
        tools=[
            {
                "type": "custom",
                "name": "lookup",
                "description": "Look a fact up",
                "input_schema": {"type": "object", "properties": {}},
            },
            *tools,
        ]
        if not cli
        else list(tools),
    )
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply(
            "GET",
            f"/v1/agents/{ma_id}",
            httpx.Response(200, json=native.model_dump(mode="json", exclude_unset=True)),
        )
    )
    async with trace.client() as client:
        actual = await retrieve_agent(client, native.id, scope=scope)
        source = await load_backend_agent(
            client, sessions, tenant_id=identity.tenant_id, agent_name=name, actual_agent=actual
        )
        if memory_store:
            async with sessions.begin() as db:
                await agent_memory_stores.insert_memory_store(
                    db,
                    tenant_id=identity.tenant_id,
                    agent_id=source.agent_id,
                    memory_store_id=f"memstore-{name}",
                )
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
            tool_safety=OPEN_TOOL_SAFETY,
        )
    return source, actual


def any_agent(grant):
    return grant.model_copy(update={"worker_agents": "any"})


def test_worker_agents_field_has_three_explicit_states(captured):
    _, _, grant, _, _, _ = captured
    root, other = uuid4(), uuid4()
    legacy = AgentFleetGrant.model_validate_json(grant.model_dump_json())
    assert legacy.worker_agents is None and "worker_agents" not in legacy.model_dump()
    assert worker_agent_allowed(legacy, root, root)  # the root is always allowed
    assert not worker_agent_allowed(legacy, root, other)  # absent: never silently widened
    anyone = any_agent(grant)
    assert worker_agent_allowed(anyone, root, other)
    listed = grant.model_copy(update={"worker_agents": (other,)})
    assert worker_agent_allowed(listed, root, other)
    assert not worker_agent_allowed(listed, root, uuid4())
    for bad in ((), (other, other)):
        with pytest.raises(ValueError, match="nonempty list of unique"):
            AgentFleetGrant.model_validate({**grant.model_dump(mode="json"), "worker_agents": bad})
    assert worker_agents_line(anyone, {}) == "Workers can be any agent you can use here."
    assert worker_agents_line(legacy, {}) == "Workers run as this agent only."
    assert (
        worker_agents_line(listed, {other: "researcher"})
        == "Workers can be this agent or: researcher."
    )


async def test_a_worker_runs_as_the_named_agent_with_its_own_ceiling(captured, owned):
    source, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    worker_root = agent_worker_permissions(
        root, researcher, as_researcher, any_agent(grant), policy=policy, now=NOW
    )
    # Its own agent, prompt and tools; the root's run, subject and finite limits.
    assert worker_root.agent_id == researcher.agent_id != root.agent_id
    assert worker_root.spec.system == RESEARCHER_SYSTEM
    assert [tool.name for tool in worker_root.spec.tools] == ["lookup"]
    assert worker_root.source_revisions == ((researcher.source.ref, researcher.source.revision),)
    assert (worker_root.fleet, worker_root.subject, worker_root.limits) == (
        root.fleet,
        root.subject,
        root.limits,
    )
    worker = resolve_worker_spec(
        worker_root,
        researcher.source,
        policy=policy,
        now=NOW,
        captured_source=researcher,
        resources=worker_root.resources,
        environment=worker_root.environment,
        limits=worker_root.limits,
    )
    assert worker.spec.name == "researcher"
    assert RESEARCHER_SYSTEM in worker.spec.system and "Protected source" not in worker.spec.system
    assert worker.permissions.agent_id == researcher.agent_id
    assert not worker.permissions.subject.is_admin


async def test_a_legacy_grant_never_lets_workers_run_as_another_agent(captured, owned):
    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    with pytest.raises(WorkerSpecRefused, match="scope: not_subset"):
        agent_worker_permissions(root, researcher, as_researcher, grant, policy=policy, now=NOW)
    listed = grant.model_copy(update={"worker_agents": (researcher.agent_id,)})
    assert agent_worker_permissions(
        root, researcher, as_researcher, listed, policy=policy, now=NOW
    ).agent_id == (researcher.agent_id)


async def test_a_worker_agent_runs_on_its_own_configured_model_not_the_grants_list(captured, owned):

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    pricey, actual = await capture_agent(
        owned, fleet.scope, ma_id="pricey-source", name="pricey", model="claude-opus-5-5"
    )
    assert pricey.effective_spec is not None
    as_pricey = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    opus = ModelRef(provider="anthropic", id="claude-opus-5-5")
    assert opus not in grant.models
    # No backend setting: the agent's own native model, though the grant never lists it.
    native = agent_worker_permissions(
        root, pricey, as_pricey, any_agent(grant), policy=policy, now=NOW
    )
    assert native.models == (opus,)
    # A Gemini agent config picks that model, as long as the tenant enables Gemini.
    flash = agent_worker_backend(pricey.effective_spec, "gemini", None)
    assert flash[1] == ModelRef(provider="gemini", id="gemini-3.8-flash")
    gemini_off = policy.model_copy(update={"enabled_backends": ("anthropic",)})
    gemini_on = policy.model_copy(update={"enabled_backends": ("anthropic", "gemini")})
    with pytest.raises(WorkerSpecRefused, match="backend: unbound"):
        agent_worker_permissions(
            root, pricey, as_pricey, any_agent(grant), policy=gemini_off, now=NOW, backend=flash
        )
    configured = agent_worker_permissions(
        root, pricey, as_pricey, any_agent(grant), policy=gemini_on, now=NOW, backend=flash
    )
    assert configured.models[0] == flash[1] and flash[0] in configured.backend_grants
    patch = agent_worker_patch(pricey.effective_spec, "Summarize.", configured.models[0])
    assert patch is not None and patch.model == flash[1]
    assert {"mcp", "memory", "sandbox"} <= set(patch.declared_drops or ())
    # An Argon-configured agent, with a grant that lists neither model: the grant's
    # list is never the reason, and Argon is no longer channel-only.
    argon = agent_worker_backend(pricey.effective_spec, "gemini", "gemini-4-argon")
    assert argon[0].model == "gemini-4-argon"
    assert argon[1] not in grant.models and flash[1] not in grant.models
    as_argon = agent_worker_permissions(
        root, pricey, as_pricey, any_agent(grant), policy=gemini_on, now=NOW, backend=argon
    )
    assert as_argon.models[0] == argon[1] and argon[0] in as_argon.backend_grants


async def test_the_requester_must_be_able_to_run_that_agent_here(captured, owned):
    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    elsewhere = policy.model_copy(
        update={"agent_rules": {"researcher": AgentRule(runs_in=("another-channel",))}}
    )
    with pytest.raises(WorkerSpecRefused, match="origin"):
        agent_worker_permissions(
            root, researcher, as_researcher, any_agent(grant), policy=elsewhere, now=NOW
        )


async def test_a_worker_agent_admission_must_be_the_roots_requester_and_place(captured, owned):
    from daimon.core.authz import Subject

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    someone_else = replace(
        admission,
        agent=build_agent_ref(actual.name, actual.metadata or {}),
        subject=Subject(platform_user_id="someone-else"),
    )
    with pytest.raises(WorkerSpecRefused, match="origin: unbound"):
        agent_worker_permissions(
            root, researcher, someone_else, any_agent(grant), policy=policy, now=NOW
        )
    assert Decimal(0) < root.limits.budget_usd


def ports_for(worker_root, researcher):
    from unittest.mock import create_autospec

    from daimon.core.fleet_orchestration import FleetPorts

    ports = create_autospec(FleetPorts, instance=True)
    ports.agent_worker_parent.return_value = worker_root
    ports.agent_consented_drops.return_value = ()
    ports.capture_source.return_value = researcher
    ports.read_clone.return_value = researcher.source
    admitted = []

    class Admitted(Exception):
        pass

    async def admit(parent, resolved, **kwargs):
        admitted.append((parent, resolved))
        raise Admitted

    ports.admit.side_effect = admit
    return ports, admitted, Admitted


async def test_spawn_as_agent_admits_that_agent_with_the_task_after_its_own_prompt(captured, owned):
    from daimon.core.fleet_orchestration import FleetOrchestrator
    from mux.contracts.config import BackendConfig
    from mux.profiles import backend_selection

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    worker_root = agent_worker_permissions(
        root, researcher, as_researcher, any_agent(grant), policy=policy, now=NOW
    )
    ports, admitted, Admitted = ports_for(worker_root, researcher)
    with pytest.raises(Admitted):
        await FleetOrchestrator(ports).spawn(
            root,
            policy,
            now=NOW,
            operation_key="R1",
            role="research",
            backend=BackendConfig.model_validate(backend_selection("anthropic").model_dump()),
            limits=root.limits,
            as_agent="researcher",
            task="Find three sources on SQLite write locking.",
        )
    ports.agent_worker_parent.assert_awaited_once_with(root, "researcher")
    ((parent, resolved),) = admitted
    # Admission runs under the agent's own authority, inside the root's run.
    assert parent is worker_root and parent.fleet == root.fleet
    assert resolved.spec.name == "researcher"
    assert resolved.source_agent == (researcher.source.ref, researcher.source.revision)
    assert resolved.permissions.agent_id == researcher.agent_id
    system = resolved.spec.system
    assert system.index(RESEARCHER_SYSTEM) < system.index("Your task: Find three sources")


async def test_spawn_as_a_gemini_configured_agent_runs_on_that_agents_model(captured, owned):
    from daimon.core.fleet_orchestration import FleetOrchestrator

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    assert researcher.effective_spec is not None
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    gemini_on = policy.model_copy(update={"enabled_backends": ("anthropic", "gemini")})
    worker_root = agent_worker_permissions(
        root,
        researcher,
        as_researcher,
        any_agent(grant),
        policy=gemini_on,
        now=NOW,
        backend=agent_worker_backend(researcher.effective_spec, "gemini", None),
    )
    ports, admitted, Admitted = ports_for(worker_root, researcher)
    with pytest.raises(Admitted):
        await FleetOrchestrator(ports).spawn(
            root,
            gemini_on,
            now=NOW,
            operation_key="G1",
            role="research",
            backend=None,
            limits=root.limits,
            as_agent="researcher",
            task="Find three sources.",
        )
    ((_, resolved),) = admitted
    assert resolved.spec.model == ModelRef(provider="gemini", id="gemini-3.8-flash")
    assert resolved.backend.backend == "gemini"
    assert RESEARCHER_SYSTEM in resolved.spec.system


async def test_spawn_as_an_argon_agent_runs_only_on_an_envelope_its_budget_covers(
    captured, owned, monkeypatch
):
    from daimon.core.fleet_orchestration import FleetOrchestrator
    from daimon.core.neutral_resources import gemini_worker_cost_usd

    from .test_fleet_source_permissions import _in_argon_window

    _in_argon_window(monkeypatch)
    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    assert researcher.effective_spec is not None
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    gemini_on = policy.model_copy(update={"enabled_backends": ("anthropic", "gemini")})
    worker_root = agent_worker_permissions(
        root,
        researcher,
        as_researcher,
        any_agent(grant),
        policy=gemini_on,
        now=NOW,
        backend=agent_worker_backend(researcher.effective_spec, "gemini", "gemini-4-argon"),
    )

    async def spawn(limits, *, fill):
        ports, admitted, Admitted = ports_for(worker_root, researcher)
        with pytest.raises(Admitted):
            await FleetOrchestrator(ports).spawn(
                root,
                gemini_on,
                now=NOW,
                operation_key="A1",
                role="research",
                backend=None,
                limits=limits,
                fill_token_limits=fill,
                as_agent="researcher",
                task="Find three sources.",
            )
        ((_, resolved),) = admitted
        return resolved

    # Plain language: the fleet fills a bounded envelope at Argon's tariff.
    resolved = await spawn(root.limits, fill=True)
    assert resolved.spec.model == ModelRef(provider="gemini", id="gemini-4-argon")
    limits = resolved.limits
    assert limits.max_input_tokens is not None and limits.max_output_tokens is not None
    worst = gemini_worker_cost_usd(
        "gemini-4-argon", limits.max_input_tokens, limits.max_output_tokens
    )
    assert worst <= limits.budget_usd
    # Explicit caps whose worst case ($1.18 at Argon's rates) exceeds a $0.05 budget.
    oversized = root.limits.model_copy(
        update={
            "budget_usd": Decimal("0.05"),
            "max_input_tokens": 262144,
            "max_output_tokens": 65536,
        }
    )
    with pytest.raises(WorkerSpecRefused, match="budget: exhausted"):
        await spawn(oversized, fill=False)


async def test_an_agent_with_an_ask_tool_never_runs_as_a_worker(captured, owned):
    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned,
        fleet.scope,
        ma_id="researcher-source",
        name="researcher",
        tools=(
            {
                "type": "agent_toolset_20260401",
                "configs": [
                    {"name": "bash", "enabled": True, "permission_policy": {"type": "always_ask"}}
                ],
                "default_config": {"enabled": False, "permission_policy": {"type": "always_ask"}},
            },
        ),
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    # A worker has nobody to ask, so a gated tool stays gated: the agent is refused.
    with pytest.raises(WorkerSpecRefused, match="tools: unsupported"):
        agent_worker_permissions(
            root, researcher, as_researcher, any_agent(grant), policy=policy, now=NOW
        )
    assert agent_worker_patch(researcher.effective_spec, "  ") is None


async def test_spawn_as_agent_refuses_an_agent_outside_the_roots_run(captured, owned):
    from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    worker_root = agent_worker_permissions(
        root, researcher, as_researcher, any_agent(grant), policy=policy, now=NOW
    )
    # An owner that hands back authority bound to another run, or wider limits, is refused.
    for changed in (
        replace(worker_root, fleet=replace(worker_root.fleet, run_id=uuid4())),
        replace(
            worker_root,
            limits=worker_root.limits.model_copy(update={"budget_usd": Decimal("100")}),
        ),
    ):
        ports, admitted, _ = ports_for(changed, researcher)
        with pytest.raises(FleetRefused, match="worker_agent_unbound"):
            await FleetOrchestrator(ports).agent_worker(root, policy, agent_name="researcher")
        assert not admitted
    ports, _, _ = ports_for(worker_root, researcher)
    with pytest.raises(FleetRefused, match="worker_agent_excludes_spec"):
        await FleetOrchestrator(ports).spawn(
            root,
            policy,
            now=NOW,
            operation_key="R2",
            role="research",
            backend=None,
            limits=root.limits,
            as_agent="researcher",
            agent=root.spec,
        )


@pytest.mark.parametrize("mode", ["any", "legacy"])
async def test_fleet_agents_lists_only_agents_this_run_can_spawn_as(
    captured, owned, monkeypatch, mode
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from daimon.core.defaults import ma_index
    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.turn import runtime_manifest

    _, _, grant, fleet, policy, _ = captured
    root = compose(captured)
    _, identity, _, sessions = owned
    tenant = identity.tenant_id
    agents = [
        ma_agent(id="root-source", name="helper", tenant_id=tenant, model="claude-haiku-5-5"),
        ma_agent(
            id="researcher-source", name="researcher", tenant_id=tenant, model="claude-haiku-5-5"
        ),
        ma_agent(id="pricey-source", name="pricey", tenant_id=tenant, model="claude-opus-5-5"),
        ma_agent(id="kept-source", name="kept", tenant_id=tenant, model="claude-haiku-5-5"),
        ma_agent(id="flash-source", name="flash", tenant_id=tenant, model="claude-haiku-5-5"),
    ]
    restricted = policy.model_copy(
        update={"agent_rules": {"kept": AgentRule(runs_in=("another-channel",))}}
    )
    value = any_agent(grant) if mode == "any" else grant

    async def listing(client, *, tenant_id):
        assert tenant_id == tenant
        return agents

    async def read_grant(deps, admission, *, now):
        return value

    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_backends

    flash = derive_agent_uuid(tenant_id=tenant, ma_agent_id="flash-source")

    async def setting(db, tenant_id, agent_id, **_):
        return SimpleNamespace(backend="gemini", model=None) if agent_id == flash else None

    monkeypatch.setattr(agent_backends, "current", setting)
    monkeypatch.setattr(ma_index, "list_agents_by_tenant", listing)
    monkeypatch.setattr(runtime_manifest, "read_agent_fleet_grant", read_grant)
    ports = HostFleetPorts(
        sessions,
        prices={},
        markup=Decimal(1),
        deps=SimpleNamespace(anthropic=None, fleet_enabled=False),
        preparation=lambda parent: None,
    )

    @asynccontextmanager
    async def root_request(parent, deadline=None):
        yield SimpleNamespace(admission=SimpleNamespace())

    monkeypatch.setattr(ports, "_load_preparation", root_request)
    listed = await ports.worker_agents(root, restricted)
    names = [row["name"] for row in listed]
    # Each agent's own model counts, never the grant's list.
    assert names == (["helper", "flash", "pricey", "researcher"] if mode == "any" else ["helper"])
    assert listed[0]["root"] is True and listed[0]["model"] == "claude-haiku-5-5"
    if mode == "any":
        rows = {row["name"]: row for row in listed}
        assert rows["pricey"]["price_per_million_tokens"] == "input $4.0, output $20.0"
        assert (rows["flash"]["backend"], rows["flash"]["model"]) == ("gemini", "gemini-3.8-flash")
        # A backend the tenant has not enabled is not offered.
        gemini_off = restricted.model_copy(update={"enabled_backends": ("anthropic",)})
        assert "flash" not in {row["name"] for row in await ports.worker_agents(root, gemini_off)}


@pytest.mark.parametrize("fault", [None, "refused", "foreign_capture"])
async def test_prepare_worker_agent_decides_admission_again_for_that_agent(
    captured, owned, monkeypatch, fault
):
    from types import SimpleNamespace

    from daimon.core import mux_compat
    from daimon.core.channel_backend import AgentBackendRefused
    from daimon.core.scope import ResolvedConfig
    from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
    from daimon.core.turn import admission as admission_module
    from daimon.core.turn import runtime_manifest
    from daimon.core.turn.admission import Admission, prepare_worker_agent
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.prepare import ProviderPreparationRequest

    source, admission, _, fleet, _, _ = captured
    _, identity, actor, sessions = owned
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    root_agent = ma_agent(id="root-source", name="helper", tenant_id=identity.tenant_id)
    root = ProviderPreparationRequest(
        SimpleNamespace(anthropic=None, sessionmaker=sessions, tool_safety=None),
        Admission(
            actor.account_id,
            root_agent,
            ma_environment(tenant_id=identity.tenant_id),
            ResolvedConfig(
                agent_name="helper",
                environment_name="default",
                responder_ma_agent_id="root-source",
            ),
            grant=admission,
            asks_before_publishing=True,
            backend_revision_origin="agent",
            origin_seal_ids=frozenset({"root-seal"}),
        ),
        fleet.scope,
        identity.tenant_id,
        "discord",
        "member",
        "thread",
        actor.account_id,
        False,
        DEFAULT_MA_CAPABILITIES,
        None,
        NOW + timedelta(minutes=5),
        lambda: NOW,
    )
    decided = []

    async def retrieve(client, agent_id, *, scope):
        assert agent_id == "researcher-source" and scope == fleet.scope
        return actual

    async def reauthorize(deps, again):
        decided.append(again)
        if fault == "refused":
            raise AdmissionDenied(reason="invoker_not_allowed")
        return again

    async def capture(request):
        assert request.admission.agent is actual and request.source is None
        if fault == "foreign_capture":
            return researcher.model_copy(update={"agent_id": uuid4()})
        return researcher

    async def rules(*args, **kwargs):
        return SimpleNamespace(tools=frozenset({"post_message"}), needs_driver=True)

    async def skills(*args, **kwargs):
        return ()

    monkeypatch.setattr(mux_compat, "retrieve_agent", retrieve)
    monkeypatch.setattr(admission_module, "reauthorize", reauthorize)
    monkeypatch.setattr(runtime_manifest, "capture_preparation_source", capture)
    monkeypatch.setattr(admission_module, "daimon_rule_set_for", rules)
    monkeypatch.setattr(admission_module, "turn_channel_skills", skills)
    if fault == "refused":
        with pytest.raises(AdmissionDenied):
            await prepare_worker_agent(root.deps, root, researcher)
        return
    if fault == "foreign_capture":
        with pytest.raises(AgentBackendRefused):
            await prepare_worker_agent(root.deps, root, researcher)
        return
    worker = await prepare_worker_agent(root.deps, root, researcher)
    (again,) = decided
    # The same requester, run place and scope; the agent and its facts are the worker's own.
    assert worker.admission.agent is actual and again.agent is actual
    assert worker.admission.grant.subject == admission.subject
    assert worker.admission.grant.run_place == admission.run_place
    assert worker.admission.grant.agent != admission.agent
    assert worker.admission.config.agent_name == "researcher"
    assert worker.admission.config.responder_ma_agent_id == actual.id
    assert worker.admission.daimon_rule_tools == frozenset({"post_message"})
    assert not worker.admission.asks_before_publishing
    assert worker.admission.backend_revision_origin is None
    assert worker.admission.origin_seal_ids == frozenset({"root-seal"})
    assert worker.scope == root.scope and worker.source is researcher
    assert root.admission.agent is root_agent  # the root's own request is untouched
    # Memory the root may only read stays read-only for its worker, even when the
    # seals that made it so are not in origin_seal_ids (inherited earlier).
    for root_read_only in (True, False):
        unsealed = replace(
            root,
            admission=replace(
                root.admission, memory_read_only=root_read_only, origin_seal_ids=frozenset()
            ),
        )
        worker = await prepare_worker_agent(root.deps, unsealed, researcher)
        assert worker.admission.memory_read_only is root_read_only


@pytest.mark.parametrize("provisioned", [True, False])
async def test_a_never_run_agent_gets_its_memory_store_and_its_worker_reads_it(
    captured, owned, monkeypatch, provisioned
):
    """CLI- and chat-created agents get their store on their first session (r13b)."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from daimon.core import channel_backend, memory_resource
    from daimon.core.fleet_orchestration import FleetRefused, HostFleetPorts
    from daimon.core.turn import admission as admission_module
    from daimon.core.turn import runtime_manifest

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    _, identity, _, sessions = owned
    fresh, actual = await capture_agent(
        owned, fleet.scope, ma_id="fresh-source", name="fresh", memory_store=False
    )
    assert fresh.memory_store_id is None
    warm = fresh.model_copy(update={"memory_store_id": "memstore-fresh"})
    as_fresh = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    provisions, prepared = [], []

    async def load(client, sessionmaker, *, tenant_id, agent_name):
        assert agent_name == "fresh"
        return fresh

    async def read_grant(deps, admitted, *, now):
        # The host decides on the wall clock; keep the fixture's grant current.
        value = any_agent(grant)
        limits = value.limits.model_copy(update={"deadline": now + timedelta(days=1)})
        return value.model_copy(update={"limits": limits})

    async def prepare(deps, request, source):
        # Captured before (no store yet) and again after the store exists.
        prepared.append(source)
        captured_source = fresh if len(prepared) == 1 or not provisioned else warm
        return SimpleNamespace(source=captured_source, admission=SimpleNamespace(grant=as_fresh))

    async def ensure(client, sessionmaker, **kwargs):
        provisions.append(kwargs)
        if not provisioned:
            from daimon.core.errors import StoreError

            raise StoreError("memory store create failed")

    monkeypatch.setattr(channel_backend, "load_backend_agent", load)
    monkeypatch.setattr(runtime_manifest, "read_agent_fleet_grant", read_grant)
    monkeypatch.setattr(admission_module, "prepare_worker_agent", prepare)
    monkeypatch.setattr(memory_resource, "ensure_memory_store_and_mount", ensure)
    ports = HostFleetPorts(
        sessions,
        prices={},
        markup=Decimal(1),
        deps=SimpleNamespace(anthropic=None, sessionmaker=sessions, fleet_enabled=False),
        preparation=lambda parent: None,
    )

    @asynccontextmanager
    async def root_request(parent, deadline=None):
        yield SimpleNamespace(tenant_id=identity.tenant_id, admission=SimpleNamespace())

    monkeypatch.setattr(ports, "_load_preparation", root_request)
    if not provisioned:
        with pytest.raises(FleetRefused, match="worker_agent_memory_unavailable"):
            await ports.agent_worker_parent(root, "fresh")
        return
    worker_root = await ports.agent_worker_parent(root, "fresh")
    # Provisioned once, the way the agent's own first session would, then captured again.
    assert [(p["agent_id"], p["read_only"]) for p in provisions] == [(fresh.agent_id, True)]
    assert len(prepared) == 2
    (mount,) = [r for r in worker_root.resources if r.resource.kind == "memory_store"]
    assert mount.resource.id == "memstore-fresh"
    assert worker_root.memory_read_only is True


@pytest.mark.parametrize("refusal", ["policy", "grant"])
async def test_no_memory_store_is_created_for_an_agent_the_requester_may_not_use(
    captured, owned, monkeypatch, refusal
):
    """The store is made only after the grant and a fresh policy decision allow the agent."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from daimon.core import channel_backend, memory_resource, mux_compat
    from daimon.core.fleet_orchestration import FleetRefused, HostFleetPorts
    from daimon.core.scope import ResolvedConfig
    from daimon.core.session_compat import DEFAULT_MA_CAPABILITIES
    from daimon.core.turn import admission as admission_module
    from daimon.core.turn import runtime_manifest
    from daimon.core.turn.admission import Admission
    from daimon.core.turn.errors import AdmissionDenied
    from daimon.core.turn.prepare import ProviderPreparationRequest

    _, admission, grant, fleet, _, _ = captured
    root = compose(captured)
    _, identity, actor, sessions = owned
    fresh, actual = await capture_agent(
        owned, fleet.scope, ma_id="fresh-source", name="fresh", memory_store=False
    )
    deps = SimpleNamespace(
        anthropic=None, sessionmaker=sessions, tool_safety=None, fleet_enabled=False
    )
    request = ProviderPreparationRequest(
        deps,
        Admission(
            actor.account_id,
            ma_agent(id="root-source", name="helper", tenant_id=identity.tenant_id),
            ma_environment(tenant_id=identity.tenant_id),
            ResolvedConfig(
                agent_name="helper", environment_name="default", responder_ma_agent_id="root-source"
            ),
            grant=admission,
        ),
        fleet.scope,
        identity.tenant_id,
        "discord",
        "member",
        "thread",
        actor.account_id,
        False,
        DEFAULT_MA_CAPABILITIES,
        None,
        NOW + timedelta(minutes=5),
        lambda: NOW,
    )
    provisions, decided = [], []

    async def load(client, sessionmaker, *, tenant_id, agent_name):
        return fresh

    async def read_grant(deps, admitted, *, now):
        value = grant if refusal == "grant" else any_agent(grant)
        limits = value.limits.model_copy(update={"deadline": now + timedelta(days=1)})
        return value.model_copy(update={"limits": limits})

    async def retrieve(client, agent_id, *, scope):
        return actual

    async def reauthorize(deps, again):
        # The current policy says this requester may not run that agent here.
        decided.append(again)
        raise AdmissionDenied(reason="invoker_not_allowed")

    async def rules(*args, **kwargs):
        return SimpleNamespace(tools=frozenset(), needs_driver=False)

    async def skills(*args, **kwargs):
        return ()

    async def ensure(*args, **kwargs):
        provisions.append(kwargs)

    monkeypatch.setattr(channel_backend, "load_backend_agent", load)
    monkeypatch.setattr(runtime_manifest, "read_agent_fleet_grant", read_grant)
    monkeypatch.setattr(mux_compat, "retrieve_agent", retrieve)
    monkeypatch.setattr(admission_module, "reauthorize", reauthorize)
    monkeypatch.setattr(admission_module, "daimon_rule_set_for", rules)
    monkeypatch.setattr(admission_module, "turn_channel_skills", skills)
    monkeypatch.setattr(memory_resource, "ensure_memory_store_and_mount", ensure)
    ports = HostFleetPorts(
        sessions, prices={}, markup=Decimal(1), deps=deps, preparation=lambda parent: None
    )

    @asynccontextmanager
    async def root_request(parent, deadline=None):
        yield request

    monkeypatch.setattr(ports, "_load_preparation", root_request)
    expected = (
        "worker_agent_not_in_grant" if refusal == "grant" else "worker_agent_not_allowed_here"
    )
    with pytest.raises(FleetRefused, match=expected):
        await ports.agent_worker_parent(root, "fresh")
    assert provisions == []
    # The grant check comes before any admission work; the policy check is the real one's.
    assert len(decided) == (0 if refusal == "grant" else 1)


# What `daimon agents create` makes and Managed Agents reads back: the base toolset
# with resolved controls, and a model object carrying its speed (a native control).
CLI_TOOLSET = {
    "type": "agent_toolset_20260401",
    "configs": [
        {"name": name, "enabled": True, "permission_policy": {"type": "always_allow"}}
        for name in ("bash", "read", "edit", "grep", "glob", "write")
    ],
    "default_config": {"enabled": False, "permission_policy": {"type": "always_allow"}},
}
CLI_MODEL = {"id": "claude-sonnet-5-5", "speed": "standard"}


async def test_a_cli_created_agent_worker_rederives_exactly_at_dispatch(captured, owned):
    """r14: fleet-claude was admitted, then refused at dispatch with a ValidationError."""
    from daimon.core.fleet_orchestration import FleetOrchestrator
    from daimon.core.turn.prepare import clone_overrides
    from mux.contracts.config import BackendConfig
    from mux.contracts.resources import AgentPatch
    from mux.profiles import backend_selection
    from pydantic import ValidationError

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    claude, actual = await capture_agent(
        owned,
        fleet.scope,
        ma_id="fleet-claude-source",
        name="fleet-claude",
        model=CLI_MODEL,
        tools=(CLI_TOOLSET,),
        cli=True,
    )
    assert claude.effective_spec is not None and claude.effective_spec.extensions
    as_claude = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    # Its own model (Sonnet) runs though the grant doesn't list it.
    worker_root = agent_worker_permissions(
        root, claude, as_claude, any_agent(grant), policy=policy, now=NOW
    )
    ports, admitted, Admitted = ports_for(worker_root, claude)
    with pytest.raises(Admitted):
        await FleetOrchestrator(ports).spawn(
            root,
            policy,
            now=NOW,
            operation_key="C1",
            role="claude",
            backend=BackendConfig.model_validate(backend_selection("anthropic").model_dump()),
            limits=root.limits,
            as_agent="fleet-claude",
            task="Pick one database.",
        )
    ((parent, resolved),) = admitted
    assert resolved.spec.extensions  # the agent's own speed travels with it
    # The old dispatch read passed the spec's list of extensions as a patch: this
    # is the ValidationError r14 hit.
    with pytest.raises(ValidationError):
        AgentPatch.model_validate(resolved.spec.model_dump(mode="json"))
    # Dispatch re-derives the worker from its pinned source exactly (for_worker).
    again = resolve_worker_spec(
        parent,
        claude.source,
        overrides=clone_overrides(resolved.spec),
        policy=policy,
        now=NOW,
        backend=BackendConfig.model_validate(
            resolved.backend.model_dump(include=set(BackendConfig.model_fields))
        ),
        resources=resolved.resources,
        environment=resolved.environment,
        limits=resolved.limits,
        captured_source=claude,
    )
    assert again.digest == resolved.digest


@pytest.mark.parametrize("backend", ["gemini", "openai"])
async def test_a_foreign_backend_agent_worker_declares_what_it_cannot_carry(
    captured, owned, backend
):
    """r14: fleet-openai and fleet-gemini refused with 'worker tools: unsupported'."""
    from daimon.core.fleet_orchestration import FleetOrchestrator

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    agent, actual = await capture_agent(
        owned,
        fleet.scope,
        ma_id=f"fleet-{backend}-source",
        name=f"fleet-{backend}",
        model=CLI_MODEL,
        tools=(CLI_TOOLSET,),
        cli=True,
    )
    assert agent.effective_spec is not None
    as_agent = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    on = policy.model_copy(update={"enabled_backends": ("anthropic", "openai", "gemini")})
    worker_root = agent_worker_permissions(
        root,
        agent,
        as_agent,
        any_agent(grant),
        policy=on,
        now=NOW,
        backend=agent_worker_backend(agent.effective_spec, backend, None),
    )
    ports, admitted, Admitted = ports_for(worker_root, agent)
    # The losses its owner accepted when switching it to this backend.
    ports.agent_consented_drops.return_value = ("skills",)
    with pytest.raises(Admitted):
        await FleetOrchestrator(ports).spawn(
            root,
            on,
            now=NOW,
            operation_key="F1",
            role=backend,
            backend=None,
            limits=root.limits,
            as_agent=f"fleet-{backend}",
            task="Pick one database.",
        )
    ((_, resolved),) = admitted
    assert resolved.spec.model.provider == backend
    drops = set(resolved.spec.declared_drops)
    # Accepted, foreign-provider and model-control losses, plus the native toolset
    # that this provider's worker can't carry: declared, never silently widened.
    assert {"skills", "mcp", "memory", "sandbox", "model_controls", "tools"} <= drops
    assert not resolved.spec.tools or all(t.permission == "auto" for t in resolved.spec.tools)
    assert RESEARCHER_SYSTEM in resolved.spec.system
    # Dispatch re-derives exactly this worker, declared losses included (for_worker).
    from daimon.core.turn.prepare import clone_overrides
    from mux.contracts.config import BackendConfig

    again = resolve_worker_spec(
        worker_root,
        agent.source,
        overrides=clone_overrides(resolved.spec),
        policy=on,
        now=NOW,
        backend=BackendConfig.model_validate(
            resolved.backend.model_dump(include=set(BackendConfig.model_fields))
        ),
        resources=resolved.resources,
        environment=resolved.environment,
        limits=resolved.limits,
        captured_source=agent,
    )
    assert again.digest == resolved.digest


@pytest.mark.parametrize("backend", ["gemini", "openai"])
async def test_an_agent_with_an_ask_tool_is_refused_on_a_foreign_backend_too(
    captured, owned, backend
):
    """Its ask tool is never dropped silently to fit another provider."""
    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    gated = {
        **CLI_TOOLSET,
        "configs": [
            {**config, "permission_policy": {"type": "always_ask"}}
            if config["name"] == "bash"
            else config
            for config in CLI_TOOLSET["configs"]
        ],
    }
    agent, actual = await capture_agent(
        owned,
        fleet.scope,
        ma_id=f"gated-{backend}-source",
        name=f"gated-{backend}",
        model=CLI_MODEL,
        tools=(gated,),
        cli=True,
    )
    assert agent.effective_spec is not None
    as_agent = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    on = policy.model_copy(update={"enabled_backends": ("anthropic", "openai", "gemini")})
    with pytest.raises(WorkerSpecRefused, match="tools: unsupported"):
        agent_worker_permissions(
            root,
            agent,
            as_agent,
            any_agent(grant),
            policy=on,
            now=NOW,
            backend=agent_worker_backend(agent.effective_spec, backend, None),
        )


async def test_the_root_may_declare_more_losses_for_an_agent_worker_but_never_gains(
    captured, owned
):
    from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused
    from mux.contracts.config import BackendConfig
    from mux.profiles import backend_selection

    _, admission, grant, fleet, policy, _ = captured
    root = compose(captured)
    researcher, actual = await capture_agent(
        owned, fleet.scope, ma_id="researcher-source", name="researcher"
    )
    as_researcher = replace(admission, agent=build_agent_ref(actual.name, actual.metadata or {}))
    worker_root = agent_worker_permissions(
        root, researcher, as_researcher, any_agent(grant), policy=policy, now=NOW
    )
    ports, admitted, Admitted = ports_for(worker_root, researcher)
    spawn = dict(
        now=NOW,
        operation_key="D1",
        role="research",
        backend=BackendConfig.model_validate(backend_selection("anthropic").model_dump()),
        limits=root.limits,
        as_agent="researcher",
        task="Summarize.",
    )
    with pytest.raises(Admitted):
        await FleetOrchestrator(ports).spawn(root, policy, as_agent_drops=("tools",), **spawn)
    ((_, resolved),) = admitted
    assert "tools" in resolved.spec.declared_drops and not resolved.spec.tools
    # Only whole-capability losses; nothing that names a tool to keep.
    with pytest.raises(FleetRefused, match="worker_agent_drops_unsupported"):
        await FleetOrchestrator(ports).spawn(
            root, policy, as_agent_drops=("mcp_tool:x:y",), **spawn
        )


def test_a_worker_validation_failure_logs_its_fields_never_its_values():
    from daimon.core.fleet_orchestration import _validation_categories
    from mux.contracts.resources import AgentPatch
    from pydantic import ValidationError

    secret = "sk-not-for-logs"
    with pytest.raises(ValidationError) as failure:
        AgentPatch.model_validate({"extensions": [{"namespace": secret}], "name": 7})
    categories = _validation_categories(failure.value)
    assert categories == ["extensions:dict_type", "name:string_type"]
    assert secret not in repr(categories)
    assert _validation_categories(RuntimeError(secret)) is None


async def test_a_member_forks_across_models_under_the_default_grant_without_an_admin(captured):
    """ROOT J10 policy: any member may fleet_spawn within the agent's DEFAULT caps; only
    raising caps or models needs an owner/admin."""
    import uuid as _uuid

    from daimon.core.fleet_configuration import default_fleet_grant
    from daimon.core.fleet_orchestration import FleetOrchestrator
    from daimon.core.neutral_resources import task_worker_spec
    from daimon.core.stores.domain import TenantBackendPolicy

    source, admission, _, fleet, policy, trace = captured
    assert not admission.subject.is_admin and admission.subject.platform_user_id == "member"
    # The computed default (epoch 0) exactly as this tenant's policy derives it.
    default = default_fleet_grant(
        TenantBackendPolicy(
            tenant_id=_uuid.uuid4(), revision="r", enabled_backends=policy.enabled_backends
        )
    )
    assert default is not None
    root = compose((source, admission, default, fleet, policy, trace))
    assert not root.subject.is_admin
    ports, admitted, Admitted = ports_for(root, source)
    for index, model in enumerate(("claude-opus-5-5", "claude-sonnet-5-5"), start=1):
        spec = task_worker_spec(root, role=f"W{index}", task="Postgres vs SQLite?", model=model)
        # The real spawn path: SPAWN_WORKER/RUN_AGENT authorize, then admission.
        with pytest.raises(Admitted):
            await FleetOrchestrator(ports).spawn(
                root,
                policy,
                now=NOW,
                operation_key=f"W{index}",
                role=f"W{index}",
                backend=None,
                limits=root.limits,
                agent=spec,
            )
    assert [resolved.spec.model.id for _, resolved in admitted] == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]
    # A model the default grant never includes is refused: widening models is the owner's.
    assert "claude-opus-5-5-fast" not in {model.id for model in root.models}
    with pytest.raises(WorkerSpecRefused, match="backend: not_subset"):
        task_worker_spec(root, role="W3", task="Go.", model="claude-opus-5-5-fast")
