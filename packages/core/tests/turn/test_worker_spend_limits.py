"""Requested financial ceilings cannot reach unsupported native execution."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from daimon.core.fleet import FleetConflict
from daimon.core.headless_runner import run_worker
from daimon.core.neutral_resources import WorkerLimits, WorkerSpecRefused
from daimon.core.stores import fleet

from . import test_agent_native_runtime as native_fixtures
from .test_agent_native_runtime import (
    application as application,
)
from .test_agent_native_runtime import (
    claimed_worker_request as claimed_worker_request,
)
from .test_agent_native_runtime import (
    clean_sdk_environment as clean_sdk_environment,
)
from .test_agent_native_runtime import (
    clone_source as clone_source,
)
from .test_agent_native_runtime import (
    db_preflight as db_preflight,
)
from .test_agent_native_runtime import (
    db_session_factory as db_session_factory,
)
from .test_agent_native_runtime import (
    owned as owned,
)
from .test_agent_native_runtime import (
    owned_database_preflight as owned_database_preflight,
)
from .test_agent_native_runtime import (
    provider as provider,
)

original_worker_request = native_fixtures.explicit_worker_request


@pytest.fixture(params=[False])
def single_input(request):
    return request.param


@pytest.fixture
def explicit_worker_request(original_worker_request, single_input):
    if not single_input:
        return original_worker_request
    request, *rest = original_worker_request
    limits = WorkerLimits.model_validate(request.worker.limits.model_dump() | {"max_turns": 1})
    return (
        replace(
            request,
            worker=replace(
                request.worker,
                limits=limits,
                permissions=replace(request.worker.permissions, limits=limits),
            ),
        ),
        *rest,
    )


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("field", ["max_model_requests", "max_input_tokens", "max_output_tokens"])
async def test_requested_unsupported_ceiling_cannot_allocate_or_consume_a_worker_input(
    claimed_worker_request, field
):
    request, root, worker, prices, owned, native_requests = claimed_worker_request
    limits = WorkerLimits.model_validate(request.worker.limits.model_dump() | {field: 1})
    candidate = replace(
        request,
        worker=replace(
            request.worker,
            limits=limits,
            permissions=replace(request.worker.permissions, limits=limits),
        ),
    )
    with pytest.raises(WorkerSpecRefused, match="budget: unsupported"):
        await run_worker(
            candidate,
            parent=root,
            worker=worker,
            trigger_message="Complete the assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert native_requests == []
    assert all(call.method == "GET" for call in owned.wire.requests)
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db,
            tenant_id=request.tenant_id,
            actor_id=root.fleet.account_id,
            run_id=worker.run_id,
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == worker.id)
    assert persisted.status == "failed" and persisted.error_code == "preparation_refused"
    assert persisted.binding_id is None and persisted.turns_used == 0
    assert persisted.reserved_usd == 0 and persisted.result is None


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("single_input", [True], indirect=True)
async def test_single_input_is_persisted_once_and_cannot_be_dispatched_again(
    claimed_worker_request,
):
    request, root, worker, prices, owned, native_requests = claimed_worker_request
    result = await run_worker(
        request,
        parent=root,
        worker=worker,
        trigger_message="Complete the assigned task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded" and result.turns_used == result.max_turns == 1
    assert sum(call.method == "POST" for call in native_requests) == 1
    with pytest.raises(FleetConflict):
        await run_worker(
            request,
            parent=root,
            worker=result,
            trigger_message="Do not send another request",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert sum(call.method == "POST" for call in native_requests) == 1


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
@pytest.mark.parametrize("bad_limit", ["max_turns", "deadline"])
async def test_soft_budget_malformed_host_bounds_refuse_before_any_native_work(
    claimed_worker_request, bad_limit
):
    request, root, worker, prices, owned, native_requests = claimed_worker_request
    values = request.worker.limits.model_dump() | {
        "native_budget_usd": Decimal("0.02"),
        "max_turns": 1,
        "deadline": request.now() + timedelta(seconds=120),
    }
    values[bad_limit] = 2 if bad_limit == "max_turns" else request.now() + timedelta(seconds=121)
    limits = WorkerLimits.model_validate(values)
    candidate = replace(request, worker=replace(request.worker, limits=limits))
    with pytest.raises(WorkerSpecRefused, match=bad_limit):
        await run_worker(
            candidate,
            parent=root,
            worker=worker,
            trigger_message="Complete the assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert native_requests == [] and owned.wire.requests == []


@pytest.mark.parametrize("provider", ["gemini"], indirect=True)
async def test_a_worker_out_of_time_inside_run_worker_reads_as_a_timeout(
    claimed_worker_request, monkeypatch
):
    """#1413 review B2: run_worker re-raises its TimeoutError as the turn ceiling.

    The worker must record a timeout, and the same raised error must read as one in
    the dispatcher too (provider_busy for Gemini), never as a refusal.
    """
    from daimon.core import headless_runner
    from daimon.core.errors import TurnError
    from daimon.core.fleet_orchestration import worker_failure_code

    request, root, worker, prices, _, native_requests = claimed_worker_request

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(headless_runner, "capture_preparation_source", slow)
    candidate = replace(request, deadline=request.now() + timedelta(milliseconds=300))
    with pytest.raises(TurnError) as raised:
        await run_worker(
            candidate,
            parent=root,
            worker=worker,
            trigger_message="Complete the assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert raised.value.kind == "ceiling"
    assert native_requests == []
    async with request.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db,
            tenant_id=request.tenant_id,
            actor_id=root.fleet.account_id,
            run_id=worker.run_id,
        )
        persisted = next(row for _, rows in graph for row in rows if row.id == worker.id)
    assert persisted.status == "failed"
    assert persisted.error_code == "preparation_deadline_exceeded"
    assert (
        worker_failure_code(raised.value, "dispatch_refused", backend=worker.backend)
        == "provider_busy"
    )
