"""Reconstruct only the exact retained root under its current admin epoch."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from daimon.core.authz import Subject
from daimon.core.channel_backend import AgentBackendService, BackendActor
from daimon.core.neutral_resources import WorkerSpecRefused
from daimon.core.turn.runtime_manifest import read_frozen_preparation, read_frozen_root

from .test_frozen_root import freeze
from .test_frozen_root import root_turn as root_turn


async def retained(value):
    deps, _, _, intent, _, scope, _, _ = value
    await freeze(value)
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    return parent


async def revoke(value):
    deps, prepared, *_ = value
    source = prepared._source
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    actor = BackendActor(source.tenant_id, prepared.admission.account_id, Subject(is_admin=True))
    preview = await service.preview_fleet_grant(source, None, now=datetime.now(UTC))
    return await service.revoke_fleet_grant(
        source,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=datetime.now(UTC),
    )


async def test_exact_request_reconstructed_from_retained_facts(root_turn):
    deps, prepared, _, _, _, scope, binding, transport = root_turn
    parent = await retained(root_turn)
    before = len(transport.requests)
    request = await read_frozen_preparation(
        deps, parent, deadline=parent.limits.deadline, now=lambda: datetime.now(UTC)
    )
    assert request.deps is deps and request.scope == scope
    assert request.admission.account_id == prepared.admission.account_id
    assert request.admission.grant == prepared.admission.grant
    assert not request.admission.grant.subject.is_admin
    assert request.admission.config == prepared.admission.config
    assert request.admission.agent.id == prepared.admission.agent.id
    assert request.admission.environment.id == prepared.admission.environment.id
    assert request.source.source.ref == parent.source_agents[0]
    assert request.source.source.revision == parent.source_revisions[0][1]
    assert request.session_account_id == UUID(scope.account_id)
    assert request.thread_id == binding.thread.thread_id
    assert request.external_user_id == "requester"
    assert request.deadline == parent.limits.deadline
    assert all(item.method == "GET" for item in transport.requests[before:])


@pytest.mark.parametrize("fault", ["scope", "subject", "run"])
async def test_foreign_preparation_parent_refuses_before_sdk_read(root_turn, fault):
    deps, _, _, _, _, _, _, transport = root_turn
    parent = await retained(root_turn)
    if fault == "scope":
        parent = replace(
            parent,
            fleet=replace(
                parent.fleet,
                scope=parent.fleet.scope.model_copy(update={"account_id": str(UUID(int=7))}),
            ),
        )
    elif fault == "subject":
        parent = replace(parent, subject=Subject(is_admin=True))
    else:
        parent = replace(parent, fleet=replace(parent.fleet, run_id=UUID(int=7)))
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_preparation(
            deps, parent, deadline=parent.limits.deadline, now=lambda: datetime.now(UTC)
        )
    assert len(transport.requests) == before


async def test_revoke_and_same_value_regrant_never_revive_retained_root(root_turn):
    deps, prepared, _, intent, _, scope, _, transport = root_turn
    parent = await retained(root_turn)
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    value = await service.read_fleet_grant(
        prepared._source.tenant_id, prepared._source.agent_id, now=datetime.now(UTC)
    )
    await revoke(root_turn)
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_preparation(
            deps, parent, deadline=parent.limits.deadline, now=lambda: datetime.now(UTC)
        )
    actor = BackendActor(
        prepared._source.tenant_id, prepared.admission.account_id, Subject(is_admin=True)
    )
    preview = await service.preview_fleet_grant(prepared._source, value, now=datetime.now(UTC))
    await service.set_fleet_grant(
        prepared._source,
        value,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=datetime.now(UTC),
    )
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    assert len(transport.requests) == before


async def test_stricter_caller_deadline_is_preserved(root_turn):
    deps = root_turn[0]
    parent = await retained(root_turn)
    bound = datetime.now(UTC) + timedelta(seconds=15)
    result = await read_frozen_preparation(
        deps, parent, deadline=bound, now=lambda: datetime.now(UTC)
    )
    assert result.deadline == bound < parent.limits.deadline


async def default_source_turn(root_turn):
    from daimon.core.neutral_resources import AgentFleetGrant
    from mux.contracts.ids import ModelRef
    from mux.profiles import backend_selection

    deps, prepared, _, intent, _, scope, _, transport = root_turn
    source = prepared._source
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    current = await service.read_fleet_grant(
        source.tenant_id, source.agent_id, now=datetime.now(UTC)
    )
    value = AgentFleetGrant.model_validate(
        current.model_dump()
        | {
            "choices": (
                (
                    backend_selection("anthropic"),
                    ModelRef(provider="anthropic", id="claude-haiku-5-5"),
                ),
                (backend_selection("openai"), ModelRef(provider="openai", id="gpt-6-luna")),
                (
                    backend_selection("gemini").model_copy(
                        update={"model": "gemini-3.5-flash-lite"}
                    ),
                    ModelRef(provider="gemini", id="gemini-3.5-flash-lite"),
                ),
            ),
            "max_workers": 2,
        }
    )
    actor = BackendActor(source.tenant_id, prepared.admission.account_id, Subject(is_admin=True))
    preview = await service.preview_fleet_grant(source, value, now=datetime.now(UTC))
    await service.set_fleet_grant(
        source,
        value,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=datetime.now(UTC),
    )
    deps = replace(deps, fleet_root_grants=None)
    prepared = replace(prepared, _source=source)
    updated = (deps, prepared, *root_turn[2:])
    return updated, value, source


async def test_default_constructor_uses_current_db_grant_and_actual_source(root_turn):
    updated, value, source = await default_source_turn(root_turn)
    deps, prepared, _, intent, _, scope, _, transport = updated
    before = len(transport.requests)
    await freeze(updated)
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    assert parent.backend_grants == value.backends and parent.models == value.models
    assert parent.spec.model.id == "claude-haiku-5-5"
    assert parent.subject == prepared.admission.grant.subject and not parent.subject.is_admin
    assert parent.source_revisions == ((source.source.ref, source.source.revision),)
    assert parent.resources[-1].id == "warm-root-memory"
    from daimon.core.stores import fleet

    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=source.tenant_id, run_id=parent.fleet.run_id
    )
    assert run.max_workers == 2 and run.total_budget_usd == value.limits.budget_usd
    assert parent.spec.declared_drops == ()
    assert parent.limits.deadline <= datetime.now(UTC) + timedelta(seconds=120)
    assert len(transport.requests) == before


async def test_no_grant_keeps_uncaptured_ordinary_root_without_new_effects(root_turn):
    from daimon.core.stores import mux_state

    deps, prepared, _, intent, _, scope, _, transport = root_turn
    await revoke(root_turn)
    before = len(transport.requests)

    async def loader(_grant, _source):
        raise AssertionError("ordinary input must not invoke a permission loader")

    value = (
        replace(deps, fleet_root_grants=loader),
        replace(prepared, _source=None, _preparation=None),
        *root_turn[2:],
    )
    await freeze(value)
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) is None
    assert len(transport.requests) == before


async def test_revoked_prepared_source_refuses_before_root_allocation(root_turn):
    from daimon.core.stores import mux_state

    deps, _, _, intent, _, scope, _, transport = root_turn
    await revoke(root_turn)
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused, match="stale"):
        await freeze(root_turn)
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) is None
    assert len(transport.requests) == before


@pytest.mark.parametrize("operation", ["read", "preview", "disabled"])
async def test_naive_grant_clock_is_typed_refused_or_disabled_noop(root_turn, operation):
    from daimon.core.channel_backend import AgentBackendRefused

    deps, prepared, *_ = root_turn
    source = prepared._source
    service = AgentBackendService(deps.sessionmaker, enabled=operation != "disabled")
    instant = datetime.now(UTC).replace(tzinfo=None)
    if operation == "disabled":
        assert (
            await service.read_fleet_grant(source.tenant_id, source.agent_id, now=instant) is None
        )
        return
    with pytest.raises(AgentBackendRefused, match="invalid fleet grant"):
        if operation == "read":
            await service.read_fleet_grant(source.tenant_id, source.agent_id, now=instant)
        else:
            value = await service.read_fleet_grant(
                source.tenant_id, source.agent_id, now=datetime.now(UTC)
            )
            await service.preview_fleet_grant(source, value, now=instant)


@pytest.mark.parametrize("field", ["model", "system", "version", "tools", "environment"])
async def test_fresh_native_drift_refuses_under_retained_source_authority(root_turn, field):
    import httpx
    from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport

    deps, prepared, *_ = root_turn
    parent = await retained(root_turn)
    native = prepared.admission.agent.model_dump(mode="json")
    environment = prepared.admission.environment.model_dump(mode="json")
    if field == "model":
        native["model"] = {"id": "claude-different"}
    elif field == "system":
        native["system"] = "Drifted instructions"
    elif field == "version":
        native["version"] = 2
    elif field == "tools":
        native["tools"] = [
            {
                "type": "custom",
                "name": "new-tool",
                "description": "New",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
    else:
        environment["config"]["packages"]["pip"] = ["unexpected"]
    trace = ScriptedTransport()
    trace.queue(
        ScriptedReply("GET", "/v1/agents/" + native["id"], httpx.Response(200, json=native))
    )
    if field == "environment":
        trace.queue(
            ScriptedReply(
                "GET",
                "/v1/environments/" + environment["id"],
                httpx.Response(200, json=environment),
            )
        )
    async with trace.client() as client:
        with pytest.raises(WorkerSpecRefused):
            await read_frozen_preparation(
                replace(deps, anthropic=client),
                parent,
                deadline=parent.limits.deadline,
                now=lambda: datetime.now(UTC),
            )
    assert trace.requests and all(item.method == "GET" for item in trace.requests)


async def test_default_host_ports_reaches_retained_original_preparation(root_turn):
    from decimal import Decimal

    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.stores.fleet import WorkerPool

    deps, _, *_ = root_turn
    parent = await retained(root_turn)
    ports = HostFleetPorts(
        deps.sessionmaker,
        prices={},
        markup=Decimal("1"),
        deps=deps,
        pool=WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=1),
    )
    try:
        source = await ports.capture_source(parent)
        assert source.source.ref == parent.source_agents[0]
        assert source.source.revision == parent.source_revisions[0][1]
        assert source.resource_scope == parent.fleet.scope
    finally:
        await ports.close()


@pytest.mark.parametrize("field", ["missing_source", "extra_admission", "invalid_account"])
async def test_malformed_retained_preparation_is_typed_refused_without_sdk_io(root_turn, field):
    from copy import deepcopy

    from daimon.core._models import MuxOperation
    from sqlalchemy import select, update

    deps, _, _, _, origin, _, _, transport = root_turn
    parent = await retained(root_turn)
    async with deps.sessionmaker.begin() as db:
        row = (
            await db.execute(
                select(MuxOperation).where(
                    MuxOperation.tenant_id == origin.tenant_id,
                    MuxOperation.key == "fleet_root:" + str(origin.id) + ":root:0",
                )
            )
        ).scalar_one()
        record = deepcopy(row.record)
        facts = record["result"]["preparation"]
        if field == "missing_source":
            del facts["source"]
        elif field == "extra_admission":
            facts["admission"]["unexpected"] = True
        else:
            facts["admission"]["account_id"] = "invalid"
        await db.execute(
            update(MuxOperation)
            .where(
                MuxOperation.tenant_id == row.tenant_id,
                MuxOperation.account_id == row.account_id,
                MuxOperation.key == row.key,
            )
            .values(record=record)
        )
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_preparation(
            deps, parent, deadline=parent.limits.deadline, now=lambda: datetime.now(UTC)
        )
    assert len(transport.requests) == before


@pytest.mark.parametrize("mode", ["unrestricted", "limited"])
def test_environment_sdk_facade_preserves_represented_network_packages_and_dates(mode):
    from daimon.core.mux_compat import sdk_environment, source_environment_record
    from daimon.testing.ma_models import ma_environment
    from mux.contracts.ids import Scope

    native = ma_environment()
    payload = native.model_dump(mode="json")
    payload["config"]["packages"] = {
        "apt": ["git"],
        "pip": ["pytest"],
        "npm": ["typescript"],
        "cargo": [],
        "gem": [],
        "go": [],
    }
    payload["config"]["networking"] = (
        {"type": "unrestricted"}
        if mode == "unrestricted"
        else {
            "type": "limited",
            "allowed_hosts": ["example.invalid"],
            "allow_mcp_servers": False,
            "allow_package_managers": False,
        }
    )
    native = type(native).model_validate(payload)
    scope = Scope(
        tenant_id="tenant", account_id="account", principal_id="host", authorization_id="verified"
    )
    captured = source_environment_record(native, scope)
    assert sdk_environment(captured).model_dump(mode="json") == native.model_dump(mode="json")
    restored = sdk_environment(captured.model_copy(update={"native": None}))
    assert restored.config.model_dump(mode="json") == native.config.model_dump(mode="json")
    assert restored.created_at == native.created_at and restored.updated_at == native.updated_at


@pytest.mark.parametrize("change", ["revoke", "expire"])
async def test_after_source_await_refuses_before_environment_read(root_turn, change):
    import httpx
    from daimon.core.errors import TurnError
    from daimon.testing.ma import MARouter
    from daimon.testing.ma_transport import ScriptedTransport

    deps, prepared, *_ = root_turn
    parent = await retained(root_turn)
    clock = [datetime.now(UTC)]
    bound = clock[0] + timedelta(seconds=1)
    router = MARouter()

    async def read_source(request, match):
        if change == "revoke":
            await revoke(root_turn)
        else:
            clock[0] = bound + timedelta(seconds=1)
        return httpx.Response(200, json=prepared.admission.agent.model_dump(mode="json"))

    router.add("GET", "/v1/agents/" + prepared.admission.agent.id, read_source)
    trace = ScriptedTransport(router=router)
    async with trace.client() as client:
        with pytest.raises((WorkerSpecRefused, TurnError)) as refused:
            await read_frozen_preparation(
                replace(deps, anthropic=client), parent, deadline=bound, now=lambda: clock[0]
            )
    if change == "expire":
        assert isinstance(refused.value, TurnError) and refused.value.kind == "ceiling"
    else:
        assert isinstance(refused.value, WorkerSpecRefused)
    assert [(item.method, item.path) for item in trace.requests] == [
        ("GET", "/v1/agents/" + prepared.admission.agent.id),
    ]


async def test_original_short_request_bound_survives_freeze_and_default_reader(root_turn):
    from decimal import Decimal

    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.stores import fleet
    from daimon.core.stores.fleet import WorkerPool

    value = (await default_source_turn(root_turn))[0]
    deps, prepared, _, intent, _, scope, _, transport = value
    original = datetime.now(UTC) + timedelta(seconds=5)
    prepared = replace(prepared, _preparation=replace(prepared._preparation, deadline=original))
    before = len(transport.requests)
    await freeze((deps, prepared, *value[2:]))
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=prepared._source.tenant_id, run_id=parent.fleet.run_id
    )
    assert parent.limits.deadline == run.deadline_at == original
    assert len(transport.requests) == before
    reconstructed = await read_frozen_preparation(
        deps, parent, deadline=original + timedelta(minutes=5), now=lambda: datetime.now(UTC)
    )
    assert reconstructed.deadline == original
    ports = HostFleetPorts(
        deps.sessionmaker,
        prices={},
        markup=Decimal("1"),
        deps=deps,
        pool=WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=1),
    )
    try:
        captured = await ports.capture_source(parent)
        assert captured.source.ref == parent.source_agents[0]
        with pytest.raises(WorkerSpecRefused):
            await read_frozen_root(
                deps, scope, intent_id=intent.id, now=original + timedelta(microseconds=1)
            )
    finally:
        await ports.close()


async def test_expired_original_request_cannot_allocate_or_publish_authority(root_turn):
    from daimon.core._models import FleetRun, MuxOperation
    from sqlalchemy import func, select

    value = (await default_source_turn(root_turn))[0]
    deps, prepared, *_ = value
    prepared = replace(
        prepared,
        _preparation=replace(
            prepared._preparation, deadline=datetime.now(UTC) - timedelta(seconds=1)
        ),
    )
    before = len(value[-1].requests)
    with pytest.raises(WorkerSpecRefused):
        await freeze((deps, prepared, *value[2:]))
    async with deps.sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(FleetRun)
                .where(FleetRun.tenant_id == prepared._source.tenant_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(MuxOperation)
                .where(MuxOperation.tenant_id == prepared._source.tenant_id)
            )
            == 0
        )
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(deps, value[5], intent_id=value[3].id, now=datetime.now(UTC))
    from decimal import Decimal

    from daimon.core.fleet_orchestration import HostFleetPorts
    from daimon.core.stores.fleet import WorkerPool

    ports = HostFleetPorts(
        deps.sessionmaker,
        prices={},
        markup=Decimal("1"),
        deps=deps,
        pool=WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=1),
    )
    try:
        with pytest.raises(WorkerSpecRefused):
            await ports.capture_source(value[2][0])
    finally:
        await ports.close()
    assert len(value[-1].requests) == before


@pytest.mark.parametrize("requested", ["2", "0.5"])
async def test_real_grant_bounds_trusted_loader_worker_budget(root_turn, requested):
    from decimal import Decimal

    from daimon.core._models import FleetRun
    from daimon.core.stores import mux_state
    from sqlalchemy import func, select

    deps, prepared, grants, intent, _, scope, _, transport = root_turn
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    current = await service.read_fleet_grant(
        prepared._source.tenant_id, prepared._source.agent_id, now=datetime.now(UTC)
    )
    assert current.limits.budget_usd == Decimal("1")
    grants[0] = replace(
        grants[0], limits=grants[0].limits.model_copy(update={"budget_usd": Decimal(requested)})
    )

    async def loader(_grant, _source):
        return grants[0]

    deps = replace(deps, fleet_root_grants=loader)
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused, match="origin: unsupported"):
        await freeze((deps, prepared, *root_turn[2:]))
    assert len(transport.requests) == before
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, scope, intent_id=intent.id) is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(FleetRun)
                .where(FleetRun.tenant_id == prepared._source.tenant_id)
            )
            == 0
        )
    unchanged = await service.read_fleet_grant(
        prepared._source.tenant_id, prepared._source.agent_id, now=datetime.now(UTC)
    )
    assert unchanged == current


@pytest.mark.parametrize("dimension", ["duration", "max_turns", "model", "narrower"])
async def test_installed_loader_cannot_publish_authority_from_evaluator_probe(root_turn, dimension):
    from decimal import Decimal

    from daimon.core._models import FleetRun, MuxOperation
    from daimon.core.neutral_resources import AgentFleetGrant
    from mux.contracts.ids import ModelRef
    from sqlalchemy import func, select

    deps, prepared, grants, intent, _, scope, _, transport = root_turn
    source = prepared._source
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    now = datetime.now(UTC)
    current = await service.read_fleet_grant(source.tenant_id, source.agent_id, now=now)
    assert current is not None
    if dimension == "duration":
        far = now + timedelta(minutes=40)
        value = AgentFleetGrant.model_validate(
            current.model_dump() | {"limits": current.limits.model_copy(update={"deadline": far})}
        )
        actor = BackendActor(
            source.tenant_id, prepared.admission.account_id, Subject(is_admin=True)
        )
        preview = await service.preview_fleet_grant(source, value, now=now)
        await service.set_fleet_grant(
            source,
            value,
            preview.setting_revision,
            actor,
            expected_preview_digest=preview.digest,
            now=now,
        )
        grants[0] = replace(grants[0], limits=grants[0].limits.model_copy(update={"deadline": far}))
        prepared = replace(prepared, _preparation=replace(prepared._preparation, deadline=far))
    elif dimension == "max_turns":
        grants[0] = replace(grants[0], limits=grants[0].limits.model_copy(update={"max_turns": 40}))
    elif dimension == "model":
        from mux.profiles import backend_selection

        model = ModelRef(provider="openai", id="gpt-6-luna")
        grants[0] = replace(
            grants[0],
            spec=grants[0].spec.model_copy(
                update={
                    "model": model,
                    "declared_drops": ("memory", "sandbox", "tools"),
                }
            ),
            backend_grants=(backend_selection("openai").model_copy(update={"model": model.id}),),
            models=(model,),
            resources=(),
            environment=None,
        )
    else:
        grants[0] = replace(
            grants[0],
            limits=grants[0].limits.model_copy(
                update={
                    "max_turns": 1,
                    "budget_usd": Decimal("0.5"),
                    "deadline": now + timedelta(seconds=5),
                }
            ),
        )
    called = False

    async def loader(_grant, _source):
        nonlocal called
        called = True
        return grants[0]

    deps = replace(deps, fleet_root_grants=loader)
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused) as refused:
        await freeze((deps, prepared, grants, *root_turn[3:]))
    assert (refused.value.field, refused.value.reason) == ("origin", "unsupported")
    assert not called and len(transport.requests) == before
    async with deps.sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(FleetRun)
                .where(FleetRun.tenant_id == source.tenant_id)
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(MuxOperation)
                .where(MuxOperation.tenant_id == source.tenant_id)
            )
            == 0
        )
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))


async def test_default_grant_duration_and_turn_count_bound_the_frozen_run(root_turn):
    from daimon.core.neutral_resources import AgentFleetGrant
    from daimon.core.stores import fleet

    deps, prepared, _, intent, _, scope, _, transport = root_turn
    source = prepared._source
    service = AgentBackendService(deps.sessionmaker, enabled=True)
    now = datetime.now(UTC)
    far = now + timedelta(minutes=40)
    current = await service.read_fleet_grant(source.tenant_id, source.agent_id, now=now)
    value = AgentFleetGrant.model_validate(
        current.model_dump()
        | {
            "limits": current.limits.model_copy(update={"deadline": far}),
        }
    )
    actor = BackendActor(source.tenant_id, prepared.admission.account_id, Subject(is_admin=True))
    preview = await service.preview_fleet_grant(source, value, now=now)
    await service.set_fleet_grant(
        source,
        value,
        preview.setting_revision,
        actor,
        expected_preview_digest=preview.digest,
        now=now,
    )
    prepared = replace(prepared, _preparation=replace(prepared._preparation, deadline=far))
    before = len(transport.requests)
    await freeze((deps, prepared, *root_turn[2:]))
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=source.tenant_id, run_id=parent.fleet.run_id
    )
    assert parent.limits.deadline == run.deadline_at
    assert 0 < (parent.limits.deadline - now).total_seconds() <= 125
    assert parent.limits.max_turns == current.limits.max_turns == 4
    assert parent.backend_grants == current.backends and parent.models == current.models
    assert len(transport.requests) == before
