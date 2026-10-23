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


async def test_a_busy_fleet_lock_defers_the_failure_record_to_the_sweep(native_worker, monkeypatch):
    """J4 failed 49 s past its deadline: the failure commit waited on a held fleet lock."""
    from daimon.core import headless_runner

    request, root, claimed, _, _ = native_worker
    monkeypatch.setattr(headless_runner, "WORKER_SETUP_LOCK_TIMEOUT", "100ms")
    monkeypatch.setattr(headless_runner, "WORKER_FAILURE_COMMIT_LOCK_TIMEOUT", "100ms")
    held, release = asyncio.Event(), asyncio.Event()

    async def hold_fleet_lock() -> None:
        async with request.deps.sessionmaker.begin() as db:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"fleet:{claimed.tenant_id}"},
            )
            held.set()
            await release.wait()

    holder = asyncio.create_task(hold_fleet_lock())
    try:
        await held.wait()
        with structlog.testing.capture_logs() as logs:
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
        assert getattr(raised.value, headless_runner.FAILURE_RECORDED)
        assert [
            e["failure_code"] for e in logs if e["event"] == "fleet.worker_failure_commit_deferred"
        ] == ["preparation_deadline_exceeded"]
    finally:
        release.set()
        await holder
    # Left for the deadline sweep, never marked running.
    assert (await _ended(request, claimed)).status == "starting"


async def test_the_setup_bound_never_cuts_a_slow_native_bind(native_worker, monkeypatch):
    """The bound covers the worker's own setup only: native allocation keeps its deadline."""
    from daimon.core import headless_runner

    request, root, claimed, _, _ = native_worker
    original = headless_runner.bind_session_impl

    async def slow_bind(*args, **kwargs):
        await asyncio.sleep(2.5)
        return await original(*args, **kwargs)

    # Longer than this fixture's own setup (admission alone is ~0.2 s), shorter than the bind.
    monkeypatch.setattr(headless_runner, "WORKER_SETUP_TIMEOUT_S", 2.0)
    monkeypatch.setattr(headless_runner, "bind_session_impl", slow_bind)
    result = await headless_runner.run_worker(
        request,
        parent=root,
        worker=claimed,
        trigger_message="Complete the task",
        prices=tariffs(root),
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded" and result.turns_used == 1


@pytest.mark.parametrize("shutting_down", [True, False])
async def test_a_shutdown_cancel_before_native_start_records_shutdown(
    native_worker, monkeypatch, shutting_down
):
    """SMOKE d75dd687: a worker cancelled by a host shutdown mid-preparation read
    "couldn't be set up on that model here". Only a cancel during a shutdown reads as
    the restart; any other failure keeps its own code."""
    from daimon.core import headless_runner
    from daimon.core.fleet import worker_error_text

    request, root, claimed, _, _ = native_worker

    async def cut_short(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(headless_runner, "capture_preparation_source", cut_short)
    if shutting_down:
        request.deps.running_turns.begin_shutdown()
    with pytest.raises(asyncio.CancelledError):
        await headless_runner.run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=asyncio.Event(),
        )
    ended = await _ended(request, claimed)
    assert ended.status == "failed"
    if shutting_down:
        assert ended.error_code == "shutdown"
        assert worker_error_text(ended.error_code) == "Daimon restarted before it finished"
    else:
        assert ended.error_code != "shutdown"


@pytest.mark.parametrize("how", ["stopped_then_refused", "cleanup_after_cancel", "unrelated"])
async def test_a_shutdown_failure_reads_restarted_whatever_error_escapes(
    native_worker, monkeypatch, how
):
    """J4 offline E2E (nc-fleetfix): a host shutdown stops a running worker by setting its
    cancel event; the OpenAI driver's interrupt then refused with no active root turn, so
    the error that escaped was that refusal and the card read "couldn't be set up". During
    a shutdown, a failure with the worker's cancel set or a cancel in its chain is the
    restart; a failure the shutdown didn't cause keeps its code."""
    from daimon.core import headless_runner
    from daimon.core.turn.errors import AdmissionDenied
    from mux.errors import UnsupportedCapability

    request, root, claimed, _, _ = native_worker
    request.deps.running_turns.begin_shutdown()
    cancel = asyncio.Event()

    async def failing(*args, **kwargs):
        if how == "stopped_then_refused":
            cancel.set()  # what HostFleetPorts.close() does to every running worker
            raise UnsupportedCapability(("host_cancel_root_identity",), "openai")
        if how == "cleanup_after_cancel":
            try:
                raise asyncio.CancelledError
            except asyncio.CancelledError as cancelled:
                raise UnsupportedCapability(("host_cancel_root_identity",), "openai") from (
                    cancelled
                )
        raise AdmissionDenied(reason="backend_unsupported")

    monkeypatch.setattr(headless_runner, "capture_preparation_source", failing)
    with pytest.raises((UnsupportedCapability, AdmissionDenied)):
        await headless_runner.run_worker(
            request,
            parent=root,
            worker=claimed,
            trigger_message="Complete the task",
            prices=tariffs(root),
            cancel=cancel,
        )
    ended = await _ended(request, claimed)
    assert ended.status == "failed"
    assert (ended.error_code == "shutdown") is (how != "unrelated")
