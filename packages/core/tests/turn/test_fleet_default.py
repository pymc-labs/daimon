"""J10: every agent has a fleet with safe default caps, and nobody has to set it up."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core._models import AgentBackendRevisionRecord
from daimon.core.agent_model_names import agent_catalog
from daimon.core.fleet_configuration import (
    DEFAULT_FLEET_BUDGET_USD,
    DEFAULT_FLEET_MAX_WORKERS,
    default_fleet_grant,
    is_default_fleet,
)
from daimon.core.fleet_orchestration import FleetRefused, HostFleetPorts
from daimon.core.neutral_resources import (
    AgentFleetGrant,
    WorkerLimits,
    WorkerSpecRefused,
    default_worker_limits,
    resolve_worker_spec,
    task_worker_spec,
)
from daimon.core.stores import fleet, tenant_ledger, tenants
from daimon.core.stores.domain import FLEET_OFF, TenantBackendPolicy
from daimon.core.turn.runtime_manifest import read_agent_fleet_grant, read_frozen_root, root_refusal
from mux.contracts.ids import ModelRef
from mux.profiles import backend_selection, selectable_models
from mux.state.operations import request_digest
from sqlalchemy import delete, func, select

from ..test_agent_backend import enable
from ..test_agent_backend import owned as owned
from ..test_agent_owner_settings import people as people
from .test_fleet_source_permissions import NOW as SOURCE_NOW
from .test_fleet_source_permissions import captured as captured
from .test_frozen_root import freeze
from .test_frozen_root import root_turn as root_turn

NOW = datetime(2026, 10, 23, 6, tzinfo=UTC)
ALL = ("anthropic", "openai", "gemini")


def policy(*providers):
    return TenantBackendPolicy(tenant_id=uuid.uuid4(), revision="r", enabled_backends=providers)


def ids(grant: AgentFleetGrant) -> set[str]:
    return {model.id for model in grant.models}


# --- The computed default -------------------------------------------------------------


def test_default_offers_every_enabled_providers_selectable_models_and_never_pro():
    grant = default_fleet_grant(policy(*ALL))
    assert grant is not None and is_default_fleet(grant)
    assert grant.max_workers == DEFAULT_FLEET_MAX_WORKERS == 4
    assert grant.limits.budget_usd == DEFAULT_FLEET_BUDGET_USD == Decimal(2)
    assert grant.limits.provider_limits_usd is None  # one shared $2 budget
    assert grant.worker_agents == "any"
    assert grant.native_root_budget_usd is None and grant.root_cash_budget_usd is None
    assert 0 < grant.max_worker_seconds <= grant.max_run_seconds
    # J3's "fork yourself across Opus 5.5, Astra and Argon" is inside the default.
    assert {"claude-opus-5-5", "gpt-6-astra", "gemini-4-argon"} <= ids(grant)
    catalog = {(entry.model.provider, entry.model.id) for entry in agent_catalog()}
    assert {(model.provider, model.id) for model in grant.models} <= catalog
    assert not any("pro" in model.id.split("-") for model in grant.models)
    for model in grant.models:
        if model.provider != "anthropic":
            assert model.id in selectable_models(model.provider)
    assert "gemini-3.5-flash-lite" not in ids(grant)  # worker-only, not selectable


def test_default_never_offers_a_provider_the_tenant_has_not_enabled():
    only = default_fleet_grant(policy("anthropic"))
    assert only is not None and {model.provider for model in only.models} == {"anthropic"}
    two = default_fleet_grant(policy("anthropic", "openai"))
    assert two is not None and {model.provider for model in two.models} == {"anthropic", "openai"}
    assert default_fleet_grant(policy()) is None


def test_default_never_offers_a_pro_model_even_if_the_catalog_had_one(monkeypatch):
    from daimon.core import agent_model_names
    from daimon.core.agent_model_names import AgentModel

    real = agent_catalog()
    monkeypatch.setattr(
        agent_model_names,
        "agent_catalog",
        lambda: (
            *real,
            AgentModel(ModelRef(provider="gemini", id="gemini-3.8-pro"), "$"),
            AgentModel(ModelRef(provider="anthropic", id="claude-opus-5-5-fast"), "$"),
        ),
    )
    grant = default_fleet_grant(policy(*ALL))
    assert grant is not None and "gemini-3.8-pro" not in ids(grant)
    assert "claude-opus-5-5-fast" not in ids(grant) and "claude-opus-5-5" in ids(grant)


def test_default_reads_the_same_every_time_so_a_frozen_root_never_goes_stale():
    first = default_fleet_grant(policy(*ALL))
    second = default_fleet_grant(policy(*ALL))
    assert first is not None and second is not None
    assert request_digest(first.model_dump(mode="json")) == request_digest(
        second.model_dump(mode="json")
    )
    assert first.expires_at > datetime.now(UTC) + timedelta(days=365)


def test_default_gives_each_worker_10_minutes_and_a_run_20_and_the_card_says_so():
    from types import SimpleNamespace
    from typing import cast

    from daimon.core.fleet_configuration import fleet_detail_lines
    from daimon.core.stores.domain import CompatibilityPreview

    grant = default_fleet_grant(policy(*ALL))
    assert grant is not None
    assert (grant.max_worker_seconds, grant.max_run_seconds) == (600, 1200)
    preview = cast(CompatibilityPreview, SimpleNamespace(fleet_grant=grant.model_dump(mode="json")))
    lines = fleet_detail_lines(preview)
    assert "Each worker: up to 10 minutes; 1 reply." in lines
    assert any(line.startswith("Each run: up to 20 minutes and ") for line in lines)


def test_cap_refusals_are_plain():
    assert "Use fewer workers" in str(FleetRefused("worker_limit_reached"))
    assert "owner or an admin" in str(FleetRefused("worker_limit_reached"))
    assert "budget" in str(FleetRefused("financial_admission_refused"))
    assert FleetRefused("worker_limit_reached").reason == "worker_limit_reached"


# --- Stored settings: default, custom, off -------------------------------------------


def custom(max_workers=2, *, deadline=NOW + timedelta(days=30)):
    return AgentFleetGrant(
        max_workers=max_workers,
        choices=(
            (backend_selection("anthropic"), ModelRef(provider="anthropic", id="claude-haiku-5-5")),
        ),
        limits=WorkerLimits(budget_usd=Decimal("0.5"), deadline=deadline, max_turns=1),
        max_run_seconds=600,
        max_worker_seconds=120,
        max_root_turns=3,
    )


async def put(service, agent, actor, value):
    preview = await service.preview_fleet_grant(agent, value, now=NOW)
    if value is None:
        return await service.revoke_fleet_grant(
            agent,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
            now=NOW,
        )
    return await service.set_fleet_grant(
        agent,
        value,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=NOW,
    )


async def revisions(sessions, agent) -> int:
    async with sessions() as db:
        return await db.scalar(
            select(func.count())
            .select_from(AgentBackendRevisionRecord)
            .where(AgentBackendRevisionRecord.agent_id == agent.agent_id)
        )


async def test_a_fresh_agent_gets_the_default_and_nothing_is_written(owned):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    assert setting.kind == "default" and setting.grant == setting.base
    assert setting.grant == default_fleet_grant(await service.tenant_policy(agent.tenant_id))
    assert await service.read_fleet_grant(agent.tenant_id, agent.agent_id, now=NOW) is None
    assert await revisions(sessions, agent) == 0


async def test_the_default_follows_the_tenants_providers(owned):
    service, agent, actor, _ = owned
    # A fresh tenant's stored allowlist: only providers it enabled, never an unkeyed one.
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    enabled = (await service.tenant_policy(agent.tenant_id)).enabled_backends
    assert {model.provider for model in setting.grant.models} <= set(enabled)
    await enable(service, agent, actor)
    policy_now = await service.tenant_policy(agent.tenant_id)
    await service.set_tenant_allowlist(agent.tenant_id, ("anthropic",), policy_now.revision, actor)
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    assert {model.provider for model in setting.grant.models} == {"anthropic"}


async def test_an_explicit_owner_tightening_wins_over_the_default(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    await put(service, agent, actor, custom(max_workers=2))
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    assert setting.kind == "custom" and setting.grant.max_workers == 2
    assert ids(setting.grant) == {"claude-haiku-5-5"}
    assert not is_default_fleet(setting.grant)


async def test_turning_fleet_off_stays_off_through_backend_changes_and_reverts(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    await put(service, agent, actor, custom())
    off = await put(service, agent, actor, None)
    assert off.fleet_grant == FLEET_OFF and off.fleet_off
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    assert setting.kind == "off" and setting.grant is None
    # An edit after "off" starts from the default limits, not from nothing.
    assert setting.base == default_fleet_grant(await service.tenant_policy(agent.tenant_id))
    switched = await service.set_backend(agent, "openai", off.local, actor)
    assert switched.fleet_off
    reverted = await service.revert(agent, actor, expected_revision=switched.local)
    assert reverted.fleet_off
    assert (await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)).grant is None
    again = await put(service, agent, actor, custom(max_workers=3))
    assert again.fleet_grant["max_workers"] == 3


async def test_turning_off_a_never_configured_agent_stays_off(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    await put(service, agent, actor, None)
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    assert setting.kind == "off" and setting.grant is None


async def test_an_expired_custom_grant_is_off_never_widened_to_the_default(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    await put(service, agent, actor, custom(deadline=NOW + timedelta(hours=1)))
    later = NOW + timedelta(hours=2)
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=later)
    assert setting.kind == "off" and setting.grant is None


async def test_a_backend_change_on_an_unconfigured_agent_keeps_the_default(owned):
    service, agent, actor, _ = owned
    await enable(service, agent, actor)
    await service.set_backend(agent, "openai", 0, actor)
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    assert setting.kind == "default" and setting.grant is not None


async def test_a_disabled_service_reads_fleet_off(owned):
    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    disabled = type(service)(sessions, enabled=False)
    setting = await disabled.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    assert setting.kind == "off" and setting.grant is None


# --- The turn path: a never-set-up channel, no grant ----------------------------------


async def unconfigured(value):
    """The root_turn fixture's agent with its grant rows removed: a fresh agent."""
    deps, prepared, *_ = value
    async with deps.sessionmaker.begin() as db:
        await db.execute(delete(AgentBackendRevisionRecord))
    grant = await read_agent_fleet_grant(deps, prepared.admission, now=datetime.now(UTC))
    assert grant is not None and is_default_fleet(grant)
    return grant


@pytest.fixture
async def memoryless_root(db_preflight, db_committing_engine):
    """A root on its agent's very first turn: its memory store doesn't exist yet."""
    async for value in root_turn.__wrapped__(db_preflight, db_committing_engine, memory=False):
        yield value


async def test_fresh_agent_spawns_three_workers_under_the_default_caps(root_turn):
    specs = await _spawn_three_default_workers(root_turn)
    # The agent's memory store exists, so its workers keep (read-only) memory.
    assert all("memory" not in spec.declared_drops for spec in specs)


async def test_a_first_ask_before_the_memory_store_exists_runs_workers_without_memory(
    memoryless_root,
):
    """The first fleet ask in a new server is admitted; its workers declare the memory loss."""
    deps, prepared, *_ = memoryless_root
    assert prepared._source.memory_store_id is None
    specs = await _spawn_three_default_workers(memoryless_root)
    assert all("memory" in spec.declared_drops for spec in specs)


async def _spawn_three_default_workers(root_turn):
    deps, prepared, _, intent, origin, scope, _, _ = root_turn
    grant = await unconfigured(root_turn)
    await freeze(root_turn)
    parent, policy_now = await read_frozen_root(
        deps, scope, intent_id=intent.id, now=datetime.now(UTC)
    )
    assert parent.limits.budget_usd == DEFAULT_FLEET_BUDGET_USD
    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=prepared.admission.grant.tenant_id, run_id=parent.fleet.run_id
    )
    assert run.max_workers == min(deps.fleet_max_workers, DEFAULT_FLEET_MAX_WORKERS)
    assert run.total_budget_usd == DEFAULT_FLEET_BUDGET_USD
    tenant_id = prepared.admission.grant.tenant_id
    async with deps.sessionmaker.begin() as db:
        await tenants.set_funding_mode(db, tenant_id=tenant_id, funding_mode="prepaid")
        await tenant_ledger.insert_entry(
            db,
            tenant_id=tenant_id,
            delta_usd=Decimal(10),
            reason="trial_credit",
            idempotency_key=f"trial:{tenant_id}",
        )
    dispatched = []

    async def dispatch(*args):
        dispatched.append(args)

    from daimon.core.pricing import provider_prices

    prices = {}
    for backend in parent.backend_grants:
        prices.update(provider_prices(backend.profile))
    ports = HostFleetPorts(deps.sessionmaker, prices=prices, markup=Decimal(1), dispatch=dispatch)
    root_model = parent.spec.model.id
    models = [model.id for model in grant.models if model.provider == "anthropic"][:3]
    assert root_model not in models or len(models) == 3
    workers, specs = [], []
    for ordinal, model in enumerate(models):
        spec = task_worker_spec(parent, role=f"W{ordinal}", task="Postgres or SQLite?", model=model)
        limits = default_worker_limits(
            parent.limits, run, tuple(workers), provider="anthropic", planned_workers=3
        )
        assert limits.budget_usd <= Decimal("0.67")  # a third of $2, in cents
        resolved = resolve_worker_spec(
            parent,
            spec,
            policy=policy_now,
            now=datetime.now(UTC),
            captured_source=prepared._source,
            resources=parent.resources,
            environment=parent.environment,
            limits=limits,
        )
        run_now, worker = await ports.admit(
            parent,
            resolved,
            run_id=run.id,
            operation_key=f"default-{ordinal}",
            role=f"W{ordinal}",
            session=None,
            result_contract=None,
            reviewer_of=None,
            reviewed_result=None,
        )
        assert worker.status != "cancelled", worker.error_code
        workers.append(worker)
        specs.append(resolved.spec)
        assert all(
            ref.resource is None or ref.resource.kind != "memory_store"
            for ref in resolved.resources
        ) == ("memory" in resolved.spec.declared_drops)
    assert len(workers) == 3 and len(dispatched) == 3
    assert root_refusal(origin.id) is None
    return specs


async def test_fleet_kill_switch_stops_the_default(root_turn):
    deps, prepared, _, intent, _, scope, _, _ = root_turn
    await unconfigured(root_turn)
    stopped = replace(deps, fleet_enabled=False)
    assert await read_agent_fleet_grant(stopped, prepared.admission, now=datetime.now(UTC)) is None


async def test_a_default_root_refusal_never_fails_the_turn(root_turn, monkeypatch):
    from daimon.core import neutral_resources

    deps, prepared, _, intent, origin, scope, _, _ = root_turn
    await unconfigured(root_turn)

    def refused(*args, **kwargs):
        raise WorkerSpecRefused("backend", "unsupported")

    monkeypatch.setattr(neutral_resources, "fleet_root_permissions", refused)
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        await freeze(root_turn)  # no raise: the turn answers with fleet off
    assert root_refusal(origin.id) == "unavailable"
    skipped = [entry for entry in logs if entry["event"] == "fleet.default_root_skipped"]
    assert skipped and skipped[0]["log_level"] == "warning"
    assert skipped[0]["cause"] == "WorkerSpecRefused"
    with pytest.raises((WorkerSpecRefused, Exception)):
        await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))


async def test_a_held_turn_under_the_default_answers_with_fleet_off(root_turn):
    from daimon.core.turn_hold import TurnHold

    deps, prepared, _, intent, origin, scope, binding, transport = root_turn
    await unconfigured(root_turn)
    hold = TurnHold(platform="discord", channel_id="channel", client=True)
    held = replace(prepared, admission=replace(prepared.admission, hold=hold))
    await freeze((deps, held, None, intent, origin, scope, binding, transport))
    assert root_refusal(origin.id) == "held"


async def test_a_plan_beyond_the_default_caps_is_refused_with_the_real_limits(root_turn):
    """J7: "40 Opus agents" is refused plainly; nothing is spawned or offered."""
    from daimon.core.fleet_orchestration import FleetOrchestrator

    deps, _, _, intent, _, scope, _, _ = root_turn
    await unconfigured(root_turn)
    await freeze(root_turn)
    parent, policy_now = await read_frozen_root(
        deps, scope, intent_id=intent.id, now=datetime.now(UTC)
    )
    orchestrator = FleetOrchestrator(
        HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1))
    )
    with pytest.raises(FleetRefused) as refused:
        await orchestrator.spawn_limits(
            parent,
            policy_now,
            operation_key="forty",
            provider="anthropic",
            now=datetime.now(UTC),
            planned_workers=40,
        )
    assert refused.value.reason == "over_fleet_limits"
    message = str(refused.value)
    assert "limited to 4 workers and $2 a run" in message
    assert "owner or an admin" in message and "don't offer a bigger limit" in message
    run, workers = await fleet.run_status(
        deps.sessionmaker, tenant_id=uuid.UUID(scope.tenant_id), run_id=parent.fleet.run_id
    )
    assert workers == () and run.reserved_usd == 0
    # Within the caps the same plan shape is fine.
    limits = await orchestrator.spawn_limits(
        parent,
        policy_now,
        operation_key="three",
        provider="anthropic",
        now=datetime.now(UTC),
        planned_workers=3,
    )
    assert limits.budget_usd <= Decimal("0.67")


async def test_an_agent_revoked_before_the_off_marker_stays_off(owned):
    """Security review #1351: a legacy revoke stored NULL after a grant; it must read off."""
    from daimon.core.stores import agent_backends

    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    granted = await put(service, agent, actor, custom())
    async with sessions.begin() as db:
        # Exactly what the old revoke wrote: the next revision, with no grant at all.
        await agent_backends.put_revision(
            db,
            tenant_id=agent.tenant_id,
            agent_id=agent.agent_id,
            local=granted.local + 1,
            backend=None,
            digest="legacy-revoke",
            previous_local=granted.previous_local,
            agent_name=agent.name,
            preview_digest="legacy-revoke",
            fleet_grant=None,
        )
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)
    assert setting.kind == "off" and setting.grant is None
    # A backend change after it carries "off" on.
    switched = await service.set_backend(agent, "openai", granted.local + 1, actor)
    assert switched.fleet_grant is None
    assert (await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)).kind == "off"


async def test_a_default_root_goes_stale_when_the_tenants_providers_change(root_turn):
    from daimon.core.stores import access_policy

    deps, prepared, _, intent, _, scope, _, _ = root_turn
    await unconfigured(root_turn)
    await freeze(root_turn)
    await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    async with deps.sessionmaker.begin() as db:
        current = await access_policy.load_access_policy(db, tenant_id=uuid.UUID(scope.tenant_id))
        narrower = tuple(p for p in current.enabled_backends if p != "gemini") or ("anthropic",)
        assert narrower != current.enabled_backends
        await access_policy.set_access_policy(
            db,
            tenant_id=uuid.UUID(scope.tenant_id),
            policy=current.model_copy(update={"enabled_backends": narrower}),
        )
    with pytest.raises(WorkerSpecRefused, match="stale"):
        await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))


async def test_any_default_freeze_failure_answers_with_fleet_off(root_turn, monkeypatch):
    from daimon.core import neutral_resources

    deps, prepared, _, intent, origin, scope, _, _ = root_turn
    await unconfigured(root_turn)

    def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(neutral_resources, "fleet_root_permissions", broken)
    await freeze(root_turn)  # no raise
    assert root_refusal(origin.id) == "unavailable"


@pytest.mark.parametrize(
    "model", ["gpt-6-astra", "gemini-4-argon", "gpt-6-luna", "gemini-3.8-flash", "claude-opus-5-5"]
)
async def test_every_default_model_has_its_backend_choice_and_resolves_as_a_worker(captured, model):
    """The default emits one backend choice per catalog model (the resolver's backend gate)."""
    from daimon.core.neutral_resources import fleet_root_permissions

    source, admission, _, fleet_facts, policy_now, _ = captured
    assert set(ALL) <= set(policy_now.enabled_backends)
    grant = default_fleet_grant(policy(*policy_now.enabled_backends))
    assert grant is not None
    root = fleet_root_permissions(
        source, admission, grant, fleet=fleet_facts, policy=policy_now, now=SOURCE_NOW
    )
    provider = next(m.provider for m in grant.models if m.id == model)
    matching = [
        backend
        for backend in root.backend_grants
        if backend.backend == provider and backend.model in (None, model)
    ]
    assert len(matching) == 1
    spec = task_worker_spec(root, role="W", task="Postgres or SQLite?", model=model)
    limits = default_worker_limits(
        root.limits, None, (), provider=provider, budget_usd=Decimal("0.5")
    )
    if model == "gemini-4-argon":
        # Argon runs only on the bounded envelope the spawn fills in.
        limits = limits.model_copy(update={"max_input_tokens": 4096, "max_output_tokens": 1024})
    child = resolve_worker_spec(
        root,
        spec,
        policy=policy_now,
        now=SOURCE_NOW,
        captured_source=source,
        resources=root.resources if provider == "anthropic" else (),
        environment=root.environment if provider == "anthropic" else None,
        limits=limits,
    )
    assert child.spec.model.id == model and child.backend == matching[0]


# --- Owners only tighten (LEAD 073727Z) ----------------------------------------------


def _raise(grant: AgentFleetGrant, dimension: str) -> AgentFleetGrant:
    """The default with exactly one dimension raised above it."""
    data = grant.model_dump(mode="json")
    limits = data["limits"]
    if dimension == "workers":
        data["max_workers"] = grant.max_workers + 1
    elif dimension == "budget":
        limits["budget_usd"] = str(grant.limits.budget_usd + 1)
    elif dimension == "model":
        lite = "gemini-3.5-flash-lite"
        data["choices"].append(
            (
                backend_selection("gemini")
                .model_copy(update={"model": lite})
                .model_dump(mode="json"),
                {"provider": "gemini", "id": lite},
            )
        )
    elif dimension == "worker_seconds":
        data["max_worker_seconds"] = grant.max_worker_seconds + 1
    elif dimension == "run_seconds":
        data["max_run_seconds"] = grant.max_run_seconds + 1
    elif dimension == "root_turns":
        data["max_root_turns"] = grant.max_root_turns + 1
    elif dimension == "turns":
        limits["max_turns"] = grant.limits.max_turns + 1
    elif dimension == "expiry":
        limits["deadline"] = (grant.expires_at + timedelta(days=1)).isoformat()
    elif dimension == "root_budget":
        data["native_root_budget_usd"] = "0.25"
        data["root_cash_budget_usd"] = "1"
    return AgentFleetGrant.model_validate_json(json.dumps(data))


@pytest.fixture
async def people_default(people):
    service, agent, actors, sessions = people
    default = default_fleet_grant(await service.tenant_policy(agent.tenant_id))
    assert default is not None
    return service, agent, actors, sessions, default


@pytest.mark.parametrize(
    "dimension",
    [
        "workers",
        "budget",
        "model",
        "worker_seconds",
        "run_seconds",
        "root_turns",
        "turns",
        "expiry",
        "root_budget",
    ],
)
async def test_an_owner_can_not_raise_any_default_limit_but_an_admin_can(people_default, dimension):
    from daimon.core.channel_backend import AgentBackendRefused

    service, agent, actors, _, default = people_default
    raised = _raise(default, dimension)
    with pytest.raises(AgentBackendRefused, match="admin"):
        await service.check_owner_fleet_ceiling(agent, raised, actors["owner"], now=NOW)
    # The write itself (what the Confirm click executes) refuses too.
    with pytest.raises(AgentBackendRefused) as refused:
        await put(service, agent, actors["owner"], raised)
    assert refused.value.reason == "admin_required"
    assert (await service.fleet_setting(agent.tenant_id, agent.agent_id, now=NOW)).kind == "default"
    # An admin may raise it.
    saved = await put(service, agent, actors["admin"], raised)
    assert saved.fleet_grant is not None


async def test_only_platform_admin_can_tighten_raise_or_revoke_worker_limits(people_default):
    from daimon.core.channel_backend import AgentBackendRefused

    service, agent, actors, _, default = people_default
    tighter = default.model_copy(update={"max_workers": 2})
    for who in ("owner", "member"):
        for value in (tighter, default, None):
            with pytest.raises(AgentBackendRefused, match="admin"):
                await put(service, agent, actors[who], value)
    await put(service, agent, actors["admin"], tighter)
    await put(service, agent, actors["admin"], default.model_copy(update={"max_workers": 8}))
    await put(service, agent, actors["admin"], None)


def test_tightening_is_checked_for_worker_agents_provider_and_token_caps():
    from daimon.core.fleet_configuration import fleet_grant_within

    base = default_fleet_grant(policy(*ALL))
    assert base is not None and fleet_grant_within(base, base)
    one, two = uuid.uuid4(), uuid.uuid4()
    listed = base.model_copy(update={"worker_agents": (one,)})
    assert fleet_grant_within(listed, base)  # "any" -> a list tightens
    assert not fleet_grant_within(base, listed)  # a list -> "any" widens
    assert not fleet_grant_within(listed.model_copy(update={"worker_agents": (one, two)}), listed)
    assert fleet_grant_within(listed.model_copy(update={"worker_agents": None}), listed)
    capped = base.model_copy(
        update={
            "limits": base.limits.model_copy(
                update={
                    "provider_limits_usd": {"anthropic": Decimal("1")},
                    "max_input_tokens": 4000,
                }
            )
        }
    )
    assert fleet_grant_within(capped, base)  # adding caps tightens
    assert not fleet_grant_within(base, capped)  # removing them widens
    more = capped.model_copy(
        update={
            "limits": capped.limits.model_copy(
                update={"provider_limits_usd": {"anthropic": Decimal("1"), "openai": Decimal("1")}}
            )
        }
    )
    assert not fleet_grant_within(more, capped)  # a new provider line widens
    bigger = capped.model_copy(
        update={"limits": capped.limits.model_copy(update={"max_input_tokens": 5000})}
    )
    assert not fleet_grant_within(bigger, capped)


# --- Only platforms that own the fleet lifecycle get the default (LEAD 084330Z) -------


@pytest.mark.parametrize("platform", ["slack", "teams", None])
async def test_slack_teams_and_unknown_roots_get_no_default_fleet(root_turn, platform):
    """Only Discord settles and delivers workers today; elsewhere they'd be paid for and lost."""
    deps, prepared, *_ = root_turn
    await unconfigured(root_turn)
    admission = replace(
        prepared.admission, grant=replace(prepared.admission.grant, platform=platform)
    )
    assert await read_agent_fleet_grant(deps, admission, now=datetime.now(UTC)) is None


async def test_discord_keeps_the_default_and_a_custom_grant_still_applies_on_slack(root_turn):
    from daimon.core.fleet_configuration import FLEET_LIFECYCLE_PLATFORMS

    deps, prepared, *_ = root_turn
    assert {"discord"} == FLEET_LIFECYCLE_PLATFORMS
    # The fixture's own root holds an operator's custom grant: it applies on Slack too.
    slack = replace(prepared.admission, grant=replace(prepared.admission.grant, platform="slack"))
    custom_grant = await read_agent_fleet_grant(deps, slack, now=datetime.now(UTC))
    assert custom_grant is not None and not is_default_fleet(custom_grant)
    # With no custom grant, Discord gets the default.
    grant = await unconfigured(root_turn)
    assert is_default_fleet(grant)


@pytest.mark.parametrize("platform", ["slack", "discord"])
async def test_a_slack_turns_preparation_never_captures_a_default_fleet_source(
    root_turn, monkeypatch, platform
):
    """Under the platform gate a Slack turn has no default fleet, so no capture (no API call)."""
    from daimon.core.turn import runtime_manifest
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn_origin import current_origin_id

    deps, prepared, _, intent, origin, _, _, _ = root_turn
    await unconfigured(root_turn)
    captured: list[object] = []

    async def capture(request, **kwargs):
        captured.append(request)
        raise WorkerSpecRefused("origin", "unbound")  # degrades to fleet off either way

    monkeypatch.setattr(runtime_manifest, "capture_preparation_source", capture)
    admission = replace(
        prepared.admission, grant=replace(prepared.admission.grant, platform=platform)
    )
    token = current_origin_id.set(origin.id)
    try:
        result = await bind_session_impl(
            deps,
            admission,
            tenant_id=admission.grant.tenant_id,
            platform=platform,
            external_user_id="requester",
            thread_id=origin.thread_id,
            session_account_id=admission.account_id,
            reuse_existing=True,
        )
    finally:
        current_origin_id.reset(token)
    assert result._source is None
    assert len(captured) == (1 if platform == "discord" else 0)
