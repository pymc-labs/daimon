"""A fleet worker's own setup fails fast in plain words instead of waiting out its run.

SMOKE 8db7ec87 J4: a named Gemini worker was claimed, never reached the provider and
sat at "starting" until the run's deadline, then took 49 s more to fail.
"""

import asyncio

import pytest
import structlog
from daimon.core.fleet import FleetConflict, worker_error_text
from daimon.core.stores import fleet
from sqlalchemy import text

from ..test_fleet_budget import tariffs
from .test_native_session_receipts import native_worker as native_worker
from .test_native_session_receipts import source_read_only as source_read_only


async def _ended(request, claimed):
    _, rows = await fleet.run_status(
        request.deps.sessionmaker, tenant_id=claimed.tenant_id, run_id=claimed.run_id
    )
    (ended,) = [row for row in rows if row.id == claimed.id]
    return ended


async def test_a_stuck_setup_step_fails_the_worker_within_the_setup_bound(
    native_worker, monkeypatch
):
    from daimon.core import headless_runner

    request, root, claimed, trace, _ = native_worker

    async def stuck(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(headless_runner, "WORKER_SETUP_TIMEOUT_S", 0.2)
    monkeypatch.setattr(headless_runner, "capture_preparation_source", stuck)
    with structlog.testing.capture_logs() as logs:
        async with asyncio.timeout(10):
            with pytest.raises(FleetConflict) as raised:
                await headless_runner.run_worker(
                    request,
                    parent=root,
                    worker=claimed,
                    trigger_message="Complete the task",
                    prices=tariffs(root),
                    cancel=asyncio.Event(),
                )
    assert raised.value.reason == "worker_setup_busy"
    ended = await _ended(request, claimed)
    assert ended.status == "failed" and ended.error_code == "preparation_deadline_exceeded"
    assert worker_error_text(ended.error_code) == "it took too long to start"
    stages = [(e["stage"], e["outcome"]) for e in logs if e["event"] == "fleet.worker_stage"]
    assert stages == [("policy", "ok"), ("source", "setup_timeout")]
    assert not any(call.method == "POST" for call in trace.requests)


async def test_a_busy_fleet_lock_fails_the_worker_instead_of_waiting(native_worker, monkeypatch):
    from daimon.core import headless_runner

    request, root, claimed, trace, _ = native_worker
    monkeypatch.setattr(headless_runner, "WORKER_SETUP_LOCK_TIMEOUT", "200ms")
    held = asyncio.Event()

    async def hold_fleet_lock() -> None:
        # Another transaction (a concurrent admission or sweep) holds the tenant fleet lock
        # well past the worker's lock wait; the failure commit then waits only for it.
        async with request.deps.sessionmaker.begin() as db:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"fleet:{claimed.tenant_id}"},
            )
            held.set()
            await asyncio.sleep(1)

    holder = asyncio.create_task(hold_fleet_lock())
    try:
        await held.wait()
        async with asyncio.timeout(5):
            with pytest.raises(FleetConflict) as raised:
                await headless_runner.run_worker(
                    request,
                    parent=root,
                    worker=claimed,
                    trigger_message="Complete the task",
                    prices=tariffs(root),
                    cancel=asyncio.Event(),
                )
        assert raised.value.reason == "worker_setup_busy"
    finally:
        await holder
    ended = await _ended(request, claimed)
    assert ended.status == "failed" and ended.error_code == "preparation_deadline_exceeded"
    assert not any(call.method == "POST" for call in trace.requests)
