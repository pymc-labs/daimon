"""Ordinary delivery and draining use the original scoped root owner."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
from daimon.core.neutral_resources import WorkerSpecRefused
from daimon.core.stores import fleet
from daimon.core.stores.fleet import WorkerPool
from daimon.core.turn.runtime_manifest import read_frozen_preparation, read_frozen_root
from mux.contracts.config import BackendConfig

from .test_frozen_preparation import revoke
from .test_frozen_root import freeze
from .test_frozen_root import root_turn as root_turn


async def test_revoked_grant_keeps_stop_but_refuses_new_worker_input(root_turn):
    deps, prepared, _, intent, _, scope, _, transport = root_turn
    await freeze(root_turn)
    original, _ = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    await revoke(root_turn)
    before = len(transport.requests)
    with pytest.raises(WorkerSpecRefused, match="stale"):
        await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    parent, policy = await read_frozen_root(
        deps, scope, intent_id=intent.id, now=datetime.now(UTC), drain=True
    )
    assert parent == original
    with pytest.raises(WorkerSpecRefused, match="stale"):
        await read_frozen_preparation(
            deps, parent, deadline=parent.limits.deadline, now=lambda: datetime.now(UTC)
        )
    ports = HostFleetPorts(
        deps.sessionmaker,
        prices={},
        markup=Decimal("1"),
        deps=deps,
        pool=WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=1),
    )
    service = FleetOrchestrator(ports)
    try:
        with pytest.raises(WorkerSpecRefused, match="stale"):
            await service.spawn(
                parent,
                policy,
                now=datetime.now(UTC),
                operation_key="revoked-new-worker",
                role="worker",
                backend=BackendConfig.model_validate(parent.backend_grants[0].model_dump()),
                limits=parent.limits,
                agent=parent.spec,
            )
        run, workers = await service.read_snapshot(parent, policy, parent.fleet.run_id)
        assert not workers
        result = await service.control(
            parent,
            policy,
            run_id=run.id,
            worker_id=None,
            action="kill",
            expected_version=run.version,
            operation_key="revoked-stop",
            now=datetime.now(UTC),
        )
        assert result["accepted"] is True
        stopped, _ = await fleet.run_status(
            deps.sessionmaker, tenant_id=prepared._source.tenant_id, run_id=run.id
        )
        assert stopped.status == "cancelled"
    finally:
        await ports.close()
    assert len(transport.requests) == before
    with pytest.raises(WorkerSpecRefused, match="stale"):
        await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC), drain=True)


@pytest.mark.parametrize("captured", [False, True])
async def test_ordinary_absence_never_downgrades_an_inflight_fleet_capture(root_turn, captured):
    from daimon.core.turn.binding import prepared_persistence

    deps, prepared, _, intent, _, scope, _, transport = root_turn
    await revoke(root_turn)
    before = len(transport.requests)
    active = await prepared_persistence(
        deps,
        prepared.admission,
        tenant_id=prepared.admission.grant.tenant_id,
        platform="discord",
        thread_id=intent.thread_id,
        session_id=prepared.ma_session_id,
        mapping_id=None,
        operation_key="ordinary-root",
        session_ref=prepared.session_ref,
        scope=scope,
        fleet_capture=captured,
    )
    assert active.source_agent_id == prepared.admission.agent.id
    assert active.session_ref == prepared.session_ref

    async def read():
        with active.activate():
            if captured:
                with pytest.raises(WorkerSpecRefused, match="unbound"):
                    await read_frozen_root(
                        deps, scope, intent_id=intent.id, now=datetime.now(UTC), allow_absent=True
                    )
            else:
                assert (
                    await read_frozen_root(
                        deps, scope, intent_id=intent.id, now=datetime.now(UTC), allow_absent=True
                    )
                    is None
                )
                with pytest.raises(WorkerSpecRefused):
                    await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))

    await active.run(read)
    with pytest.raises(WorkerSpecRefused):
        await read_frozen_root(
            deps, scope, intent_id=intent.id, now=datetime.now(UTC), allow_absent=True
        )
    assert len(transport.requests) == before


@pytest.mark.parametrize("status", ["pending", "sent", "outcome_unknown"])
async def test_ordinary_absence_refuses_every_unsettled_root_reservation(root_turn, status):
    import asyncio
    from contextlib import suppress

    from daimon.core.stores import mux_state
    from daimon.core.turn.binding import prepared_persistence
    from mux.contracts.receipts import SendReceipt

    deps, prepared, _, intent, origin, scope, _, transport = root_turn
    await revoke(root_turn)
    before = len(transport.requests)
    active = await prepared_persistence(
        deps,
        prepared.admission,
        tenant_id=prepared.admission.grant.tenant_id,
        platform="discord",
        thread_id=intent.thread_id,
        session_id=prepared.ma_session_id,
        mapping_id=None,
        operation_key="ordinary-reservation",
        session_ref=prepared.session_ref,
        scope=scope,
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def begin(key, digest):
        async with deps.sessionmaker.begin() as db:
            record = await mux_state.get_fleet_root_operation(
                db,
                scope,
                intent_id=intent.id,
                origin_id=origin.id,
                slot=active.slot,
                request_digest=digest,
            )
            assert record is not None and record.operation.key == key
            return record

    async def lost_ack(key):
        calls.append(key)
        entered.set()
        await finish.wait()
        raise OSError("unverified allocation acknowledgement")

    async def read():
        with active.activate():
            task = None
            if status == "pending":
                await begin(f"fleet_root:{origin.id}:root:0", "pending-proof")
            else:
                task = asyncio.create_task(
                    active.mutate(
                        None,
                        "root",
                        {},
                        lost_ack,
                        SendReceipt,
                        lambda receipt: "processed",
                        begin=begin,
                        operation_key=f"fleet_root:{origin.id}",
                        retry_failed=False,
                    )
                )
                await entered.wait()
                if status == "outcome_unknown":
                    finish.set()
                    with pytest.raises(OSError, match="unverified"):
                        await task
            try:
                async with deps.sessionmaker() as db:
                    record = await mux_state.get_fleet_root_operation(
                        db, scope, intent_id=intent.id, check_intent_owner=True
                    )
                    assert record.operation.status == status
                with pytest.raises(WorkerSpecRefused, match="unbound"):
                    await read_frozen_root(
                        deps, scope, intent_id=intent.id, now=datetime.now(UTC), allow_absent=True
                    )
            finally:
                finish.set()
                if task is not None:
                    with suppress(OSError):
                        await task

    await active.run(read)
    assert len(calls) == (status != "pending")
    assert len(transport.requests) == before


async def test_global_intent_absence_read_refuses_another_accounts_reservation(root_turn):
    from uuid import uuid4

    from daimon.core.stores import mux_state
    from mux.errors import ScopeViolation
    from mux.state.store import binding_slot

    deps, _, _, intent, origin, scope, binding, transport = root_turn
    before = len(transport.requests)
    async with deps.sessionmaker.begin() as db:
        original = await mux_state.get_fleet_root_operation(
            db,
            scope,
            intent_id=intent.id,
            origin_id=origin.id,
            slot=binding_slot(binding),
            request_digest="exact-intent",
        )
    other = scope.model_copy(update={"account_id": str(uuid4())})
    async with deps.sessionmaker() as db:
        assert await mux_state.get_fleet_root_operation(db, other, intent_id=intent.id) is None
        with pytest.raises(ScopeViolation):
            await mux_state.get_fleet_root_operation(
                db, other, intent_id=intent.id, check_intent_owner=True
            )
        assert (
            await mux_state.get_fleet_root_operation(
                db, scope, intent_id=intent.id, check_intent_owner=True
            )
            == original
        )
    assert len(transport.requests) == before


@pytest.mark.parametrize("selector", ["missing", "origin_id", "run_id", "request_digest"])
async def test_global_intent_absence_flag_refuses_other_selector_modes(root_turn, selector):
    from daimon.core.stores import mux_state

    deps, _, _, intent, origin, scope, _, transport = root_turn
    before = len(transport.requests)
    kwargs = (
        {}
        if selector == "missing"
        else {"intent_id": intent.id, selector: "" if selector == "request_digest" else origin.id}
    )
    async with deps.sessionmaker() as db:
        with pytest.raises(ValueError, match="intent-only"):
            await mux_state.get_fleet_root_operation(db, scope, check_intent_owner=True, **kwargs)
    assert len(transport.requests) == before


async def test_paired_price_caller_retains_complete_native_context_at_real_admission(
    root_turn, monkeypatch
):
    from datetime import date

    from daimon.core import channel_budget
    from daimon.core.fleet_orchestration import FleetRefused
    from daimon.core.neutral_resources import resolve_worker_spec
    from daimon.core.pricing import MODEL_PRICING, ProviderPrice
    from mux.profiles import backend_selection

    deps, _, _, intent, _, scope, _, transport = root_turn
    await freeze(root_turn)
    parent, policy = await read_frozen_root(deps, scope, intent_id=intent.id, now=datetime.now(UTC))
    native = ProviderPrice(
        "openai",
        "gpt-6-luna",
        date(2026, 10, 12),
        Decimal("0.1"),
        Decimal("0.5"),
        Decimal("0.01"),
        Decimal("0.125"),
    )
    supplied = {("openai", "gpt-6-luna"): native}
    ports = HostFleetPorts(
        deps.sessionmaker,
        prices=supplied,
        markup=Decimal("1"),
        deps=deps,
        pool=WorkerPool(max_concurrent_workers=1, max_concurrent_workers_per_tenant=1),
    )
    resolved = resolve_worker_spec(
        parent,
        parent.spec,
        policy=policy,
        now=datetime.now(UTC),
        backend=BackendConfig.model_validate(backend_selection("anthropic").model_dump()),
        limits=parent.limits,
    )
    observed = []
    original = channel_budget.admit_fleet_worker

    async def check(db, **kwargs):
        observed.append(kwargs["prices"])
        assert kwargs["prices"] is ports.prices
        assert kwargs["prices"][("openai", "gpt-6-luna")] is native
        assert (
            kwargs["prices"][(resolved.spec.model.provider, resolved.spec.model.id)]
            is MODEL_PRICING[resolved.spec.model.id]
        )
        return await original(db, **kwargs)

    monkeypatch.setattr(channel_budget, "admit_fleet_worker", check)
    before = len(transport.requests)
    try:
        with pytest.raises(FleetRefused, match="financial_admission_refused"):
            await ports.admit(
                parent,
                resolved,
                run_id=parent.fleet.run_id,
                operation_key="complete-price-context",
                role="worker",
                session=None,
                result_contract=None,
                reviewer_of=None,
                reviewed_result=None,
            )
    finally:
        await ports.close()
    assert len(observed) == 1
    assert supplied == {("openai", "gpt-6-luna"): native}
    assert len(transport.requests) == before
