"""Installed fleet consumers read the real frozen-root and continuation owners."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from daimon.core.authz import Subject
from daimon.core.continuity.wakes import FrozenWorkerBatch, enqueue_worker_finished
from daimon.core.fleet_orchestration import FleetOrchestrator, FleetRefused, HostFleetPorts
from daimon.core.security_audit import capture_decision
from daimon.core.stores import fleet
from daimon.core.stores.task_continuations import list_pending_continuations
from daimon.core.turn.runtime_manifest import read_frozen_root
from daimon.testing.factories import make_account
from daimon.testing.pytest_plugin import db_preflight as db_preflight

from .test_frozen_root import freeze
from .test_frozen_root import root_turn as root_turn

root_turn_fixture = root_turn.__wrapped__


@pytest.mark.parametrize("actor", ("owner", "other", "admin", "agent_key"))
async def test_canonical_scope_controller_checks_the_actual_platform_account(root_turn, actor):
    await freeze(root_turn)
    deps, _, _, intent, _, scope, _, _ = root_turn
    parent, policy = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    assert parent.fleet.scope.principal_id == "daimon"
    if actor != "owner":
        async with deps.sessionmaker.begin() as db:
            from daimon.core._models import Tenant

            tenant = await db.get(Tenant, UUID(scope.tenant_id))
            other = await make_account(db, tenant=tenant)
        # The identity input is a real separate account, not provider Scope.principal_id.
        async with deps.sessionmaker.begin() as db:
            from daimon.core._models import PlatformPrincipal

            db.add(
                PlatformPrincipal(
                    tenant_id=UUID(scope.tenant_id),
                    platform="discord",
                    external_id="other",
                    account_id=other.id,
                )
            )
        parent = replace(
            parent,
            subject=Subject(
                platform_user_id="other",
                is_admin=actor in ("admin", "agent_key"),
                via_agent_key=actor == "agent_key",
            ),
        )
    ports = HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
    with (
        capture_decision() as audit,
        pytest.raises(
            FleetRefused,
            match="empty_run" if actor in ("owner", "admin") else "controller_required",
        ),
    ):
        await FleetOrchestrator(ports).collect(parent, policy, run_id=parent.fleet.run_id)
    if actor in ("other", "agent_key"):
        assert audit.denied and audit.reason == "authz:controller_required"


async def test_wait_freezes_exact_cohorts_and_offers_only_the_completed_batch(root_turn):
    await freeze(root_turn)
    deps, prepared, _, intent, _, scope, _, _ = root_turn
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=UUID(scope.tenant_id), run_id=parent.fleet.run_id
    )
    rows = []
    async with deps.sessionmaker.begin() as db:
        for ordinal in range(2):
            rows.append(
                await fleet.add_worker(
                    db,
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    ordinal=ordinal,
                    role="worker",
                    spec=parent.spec.model_dump(mode="json"),
                    spec_digest=str(ordinal),
                    backend=parent.spec.model.provider,
                    profile=parent.backend_grants[0].profile,
                    model=parent.spec.model.id,
                    budget_usd=Decimal("0.1"),
                    deadline_at=run.deadline_at,
                    max_turns=1,
                )
            )
    ports = HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
    await ports.wait(parent, run, (rows[0],))
    await ports.wait(parent, run, (rows[1],))
    await ports.wait(parent, run, (rows[0],))
    async with deps.sessionmaker() as db:
        waits = await list_pending_continuations(
            db,
            tenant_id=run.tenant_id,
            platform=run.platform,
            thread_id=run.thread_id,
            include_future=True,
        )
    assert len(waits) == 2
    assert {
        tuple(FrozenWorkerBatch.model_validate_json(row.requested_work).worker_ids) for row in waits
    } == {(rows[0].id,), (rows[1].id,)}
    async with deps.sessionmaker.begin() as db:
        await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id, for_update=True)
        await fleet.finish(
            db,
            tenant_id=run.tenant_id,
            worker_id=rows[0].id,
            expected_version=rows[0].version,
            status="cancelled",
            error_code="cancelled",
        )
        assert await enqueue_worker_finished(
            db, tenant_id=run.tenant_id, run_id=run.id, now=datetime.now(UTC)
        )
    async with deps.sessionmaker() as db:
        offered = await list_pending_continuations(
            db, tenant_id=run.tenant_id, platform=run.platform, thread_id=run.thread_id
        )
    assert len(offered) == 1
    assert FrozenWorkerBatch.model_validate_json(offered[0].requested_work).worker_ids == (
        rows[0].id,
    )
    retained, _ = await fleet.run_status(deps.sessionmaker, tenant_id=run.tenant_id, run_id=run.id)
    assert retained.status == "running"


@pytest.mark.parametrize("target", ("run", "worker"))
async def test_control_retains_scoped_stop_without_asserting_native_termination(root_turn, target):
    await freeze(root_turn)
    deps, _, _, intent, _, scope, _, _ = root_turn
    parent, policy = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    run, _ = await fleet.run_status(
        deps.sessionmaker, tenant_id=UUID(scope.tenant_id), run_id=parent.fleet.run_id
    )
    async with deps.sessionmaker.begin() as db:
        worker = await fleet.add_worker(
            db,
            tenant_id=run.tenant_id,
            run_id=run.id,
            ordinal=0,
            role="worker",
            spec=parent.spec.model_dump(mode="json"),
            spec_digest="stored",
            backend=parent.spec.model.provider,
            profile=parent.backend_grants[0].profile,
            model=parent.spec.model.id,
            budget_usd=Decimal("0.1"),
            deadline_at=run.deadline_at,
            max_turns=1,
        )
    owner = FleetOrchestrator(
        HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
    )
    terms = dict(
        run_id=run.id,
        worker_id=worker.id if target == "worker" else None,
        expected_version=worker.version if target == "worker" else run.version,
        operation_key="stable-stop",
        action="kill",
        now=datetime.now(UTC),
    )
    first = await owner.control(parent, policy, **terms)
    assert first["accepted"] is True and first["status"] == "accepted"
    assert await owner.control(parent, policy, **terms) == first
    actual, rows = await fleet.run_status(deps.sessionmaker, tenant_id=run.tenant_id, run_id=run.id)
    assert actual.status == ("running" if target == "worker" else "cancelled")
    assert rows[0].status == worker.status == "pending"
    assert rows[0].binding_id is None and rows[0].reserved_usd == worker.reserved_usd
    assert rows[0].turns_used == worker.turns_used and rows[0].attempts == worker.attempts
    assert rows[0].error_code == ("stop_requested" if target == "worker" else None)
    with pytest.raises(FleetRefused, match="operation_conflict"):
        await owner.control(parent, policy, **(terms | {"expected_version": 999}))
    if target == "worker":
        with pytest.raises(FleetRefused, match="resume_evidence_unavailable"):
            await owner.control(
                parent,
                policy,
                **(terms | {"action": "resume", "expected_version": rows[0].version}),
            )


@pytest.mark.parametrize(
    "fault", (None, "foreign_account", "agent_key", "missing_origin", "wrong_run")
)
async def test_mcp_service_loads_real_frozen_origin_without_an_injected_parent(root_turn, fault):
    from unittest.mock import Mock

    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.runtime import McpRuntime
    from daimon.adapters.mcp.tools.fleet import HostFleetToolService
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.scope import DeploymentDefault
    from daimon.core.stores.domain import Role
    from fastmcp.exceptions import ToolError

    await freeze(root_turn)
    deps, prepared, _, intent, origin, scope, _, transport = root_turn
    parent, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    caller = AuthIdentity(
        tenant_id=UUID(scope.tenant_id),
        account_id=parent.fleet.account_id,
        role=Role.USER,
        platform=parent.fleet.root.channel.platform,
        platform_user_id="requester",
        chat_agent_id=derive_agent_uuid(
            tenant_id=UUID(scope.tenant_id), ma_agent_id=prepared.admission.agent.id
        ),
    )
    if fault == "foreign_account":
        caller = replace(caller, account_id=uuid4())
    elif fault == "agent_key":
        caller = replace(caller, agent_id=caller.chat_agent_id)
    runtime = McpRuntime(
        session_factory=deps.sessionmaker,
        client=deps.anthropic,
        settings=Mock(),
        deployment_default=DeploymentDefault(),
    )
    bridge = HostFleetToolService(
        FleetOrchestrator(
            HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
        ),
        runtime=runtime,
        deps=deps,
    )
    before = list(transport.requests)
    call = bridge.status(
        caller,
        run_id=uuid4() if fault == "wrong_run" else parent.fleet.run_id,
        origin_context_id=None if fault == "missing_origin" else origin.id,
    )
    if fault:
        with pytest.raises(ToolError, match="root_|origin_required"):
            await call
    else:
        assert (await call)["status"] == "running"
    assert list(transport.requests) == before


async def _pending(sm, parent, *, ordinal=0, reviewer_of=None):
    return await fleet.add_worker(
        sm,
        tenant_id=UUID(parent.fleet.scope.tenant_id),
        run_id=parent.fleet.run_id,
        ordinal=ordinal,
        role="worker",
        spec=parent.spec.model_dump(mode="json"),
        spec_digest=str(ordinal),
        backend=parent.spec.model.provider,
        profile=parent.backend_grants[0].profile,
        model=parent.spec.model.id,
        budget_usd=Decimal("0.1"),
        deadline_at=parent.limits.deadline,
        max_turns=1,
        reviewer_of=reviewer_of,
    )


async def test_one_shared_ports_derives_each_original_root_address(
    db_preflight, db_nullpool_engine, root_turn
):
    from contextlib import aclosing

    await freeze(root_turn)
    async with aclosing(root_turn_fixture(db_preflight, db_nullpool_engine)) as roots:
        second = await anext(roots)
        ports = HostFleetPorts(
            root_turn[0].sessionmaker, prices={}, markup=Decimal(1), deps=root_turn[0]
        )
        addresses = []
        for value in (root_turn, second):
            if value is second:
                await freeze(value)
            deps, prepared, _, intent, origin, scope, binding, _ = value
            parent, _ = await read_frozen_root(
                deps, scope, intent_id=intent.id, now=datetime.now(UTC)
            )
            run, _ = await ports.read_run(scope, parent.fleet.run_id)
            worker = await _pending(deps.sessionmaker, parent)
            # A current admin is not the original continuation requester.
            await ports.wait(
                replace(parent, subject=Subject(is_admin=True, platform_user_id="admin")),
                run,
                (worker,),
            )
            async with deps.sessionmaker() as db:
                waits = await list_pending_continuations(
                    db,
                    tenant_id=run.tenant_id,
                    platform=run.platform,
                    thread_id=run.thread_id,
                    include_future=True,
                )
            assert len(waits) == 1
            row = waits[0]
            assert (
                row.tenant_id,
                row.platform,
                row.parent_channel_id,
                row.thread_id,
                row.requester_account_id,
            ) == (
                run.tenant_id,
                run.platform,
                run.channel_id,
                run.thread_id,
                run.orchestrator_account_id,
            )
            assert row.requester_external_user_id == "requester"
            assert (row.target_ma_agent_id, row.target_name) == (
                origin.responder_ma_agent_id,
                origin.responder_name,
            )
            batch = FrozenWorkerBatch.model_validate_json(row.requested_work)
            assert (batch.binding_id, batch.session_id) == (binding.id, prepared.ma_session_id)
            addresses.append((row.tenant_id, row.requester_account_id, batch.session_id))
            with pytest.raises(FleetRefused, match="foreign_run|fleet_origin_required"):
                await ports.wait(
                    replace(
                        parent,
                        fleet=replace(
                            parent.fleet,
                            root=parent.fleet.root.model_copy(update={"thread_id": "other"}),
                        ),
                    ),
                    run,
                    (worker,),
                )
            with pytest.raises(FleetRefused, match="wait_unavailable"):
                await ports.wait(
                    replace(parent, fleet=replace(parent.fleet, origin_id=uuid4())), run, (worker,)
                )
        assert addresses[0] != addresses[1]


async def test_collect_arms_reviews_per_target_without_certifying_scheduling_rows(root_turn):
    from daimon.core._models import FleetWorker as Row
    from sqlalchemy import update

    await freeze(root_turn)
    deps, _, _, intent, _, scope, _, _ = root_turn
    parent, policy = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    first = await _pending(deps.sessionmaker, parent)
    second = await _pending(deps.sessionmaker, parent, ordinal=1)
    reviews = [
        await _pending(deps.sessionmaker, parent, ordinal=2, reviewer_of=first.id),
        await _pending(deps.sessionmaker, parent, ordinal=3, reviewer_of=second.id),
    ]
    # Scheduling inventory only: no journal result or native completion is fabricated.
    async with deps.sessionmaker.begin() as db:
        await db.execute(
            update(Row).where(Row.id.in_((first.id, second.id))).values(status="succeeded")
        )
        await db.execute(update(Row).where(Row.id == reviews[0].id).values(status="running"))
    ports = HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
    assert (await FleetOrchestrator(ports).collect(parent, policy, run_id=parent.fleet.run_id))[
        "status"
    ] == "waiting"
    async with deps.sessionmaker() as db:
        waits = await list_pending_continuations(
            db,
            tenant_id=UUID(scope.tenant_id),
            platform="discord",
            thread_id=parent.fleet.root.thread_id,
            include_future=True,
        )
        ready = await list_pending_continuations(
            db,
            tenant_id=UUID(scope.tenant_id),
            platform="discord",
            thread_id=parent.fleet.root.thread_id,
        )
    assert not ready and len(waits) == 2
    assert {
        FrozenWorkerBatch.model_validate_json(row.requested_work).worker_ids for row in waits
    } == {(review.id,) for review in reviews}
    async with deps.sessionmaker.begin() as db:
        await fleet.lock_budget(
            db,
            tenant_id=UUID(scope.tenant_id),
            actor_id=parent.fleet.account_id,
            run_id=parent.fleet.run_id,
        )
        latest = (
            await fleet.run_status(db, tenant_id=UUID(scope.tenant_id), run_id=parent.fleet.run_id)
        )[1][2]
        await fleet.finish(
            db,
            tenant_id=latest.tenant_id,
            worker_id=latest.id,
            expected_version=latest.version,
            status="failed",
            error_code="failed",
        )
        await enqueue_worker_finished(
            db, tenant_id=latest.tenant_id, run_id=latest.run_id, now=datetime.now(UTC)
        )
    async with deps.sessionmaker() as db:
        ready = await list_pending_continuations(
            db,
            tenant_id=UUID(scope.tenant_id),
            platform="discord",
            thread_id=parent.fleet.root.thread_id,
        )
    assert len(ready) == 1 and FrozenWorkerBatch.model_validate_json(
        ready[0].requested_work
    ).worker_ids == (reviews[0].id,)
    assert (await FleetOrchestrator(ports).collect(parent, policy, run_id=parent.fleet.run_id))[
        "status"
    ] == "waiting"


async def test_concurrent_identical_kills_replay_one_committed_ack(root_turn, monkeypatch):
    import asyncio

    from daimon.core.stores import mux_state

    await freeze(root_turn)
    deps, _, _, intent, _, scope, _, _ = root_turn
    parent, policy = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    ports = HostFleetPorts(deps.sessionmaker, prices={}, markup=Decimal(1), deps=deps)
    run, _ = await ports.read_run(scope, parent.fleet.run_id)
    real_read = ports.retained_control
    reads = []
    both_read = asyncio.Event()

    async def read(*args, **kwargs):
        actual = await real_read(*args, **kwargs)
        assert actual is None
        reads.append(actual)
        if len(reads) == 2:
            both_read.set()
        await both_read.wait()
        return actual

    monkeypatch.setattr(ports, "retained_control", read)
    terms = dict(
        run_id=run.id,
        worker_id=None,
        action="kill",
        expected_version=run.version,
        operation_key="simultaneous-stop",
        now=datetime.now(UTC),
    )
    owner = FleetOrchestrator(ports)
    async with asyncio.timeout(10):
        replies = await asyncio.gather(
            owner.control(parent, policy, **terms), owner.control(parent, policy, **terms)
        )
    assert replies[0] == replies[1] and replies[0]["accepted"] is True
    monkeypatch.setattr(ports, "retained_control", real_read)
    latest, _ = await ports.read_run(scope, run.id)
    assert latest.status == "cancelled" and latest.version == run.version + 1
    async with deps.sessionmaker() as db:
        record = await mux_state.get_operation(
            db, scope, f"fleet:{run.id}:control:simultaneous-stop"
        )
    assert record.operation.status == "processed" and record.result["version"] == latest.version
    with pytest.raises(FleetRefused, match="terminal_run"):
        await owner.control(
            parent,
            policy,
            **(terms | {"operation_key": "different-stop", "expected_version": latest.version}),
        )
    with pytest.raises(FleetRefused, match="operation_conflict"):
        await owner.control(parent, policy, **(terms | {"expected_version": 999}))
