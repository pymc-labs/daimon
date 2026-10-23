"""J3/J4/J10: a worker the budget watch cancels after bind reads as the cancel, never as
"Daimon refused to start it".

SMOKE 5ad12cc3 J10: Argon 78d4e5b8 bound at 22:58:32, and at 22:58:38 the watch cancelled
it (usage_unavailable). The cancelled turn ended non-COMPLETED, run_worker raised
FleetConflict(result_not_successful), and the dispatcher, which mapped the cancel's reason
only for a CancelledError, recorded dispatch_refused. The worker here is an OpenAI
conversation worker on the production dispatch path; only the watch is a stand-in that
cancels as the real one does.
"""

import asyncio
from uuid import UUID

import structlog
from daimon.core import channel_budget
from daimon.core.fleet import worker_error_text
from daimon.core.fleet_orchestration import FleetOrchestrator

from .test_fleet_native_dispatch import _settled
from .test_openai_conversation_worker import conversation_root as conversation_root
from .test_openai_conversation_worker import terms
from .test_openai_worker_plan import db_preflight as db_preflight
from .test_openai_worker_plan import owned_database_preflight as owned_database_preflight


async def test_a_worker_cancelled_after_bind_reads_as_the_cancel(conversation_root, monkeypatch):
    deps, parent, policy, resolved, wire, ports, _, _ = conversation_root
    streaming = asyncio.Event()
    handle = wire.handle

    def held(call):
        response = handle(call)
        if call.url.path.endswith("/events") and call.method == "GET":
            # The session exists and its turn is in flight: the stream answers nothing
            # until the turn is cancelled.
            wire.sent = asyncio.Event()
            streaming.set()
        return response

    monkeypatch.setattr(wire, "handle", held)

    async def watch(*, parent, cancelled, finished, **kwargs):
        # The real watch, on a lock-free read that cannot be measured.
        await streaming.wait()
        channel_budget.cancel_workers(cancelled, "usage_unavailable", run_id=parent.fleet.run_id)
        await finished.wait()

    monkeypatch.setattr(channel_budget, "watch_fleet_budget", watch)
    owner = FleetOrchestrator(ports)
    spawned = await owner.spawn(parent, policy, **terms(parent, resolved))
    task = ports._tasks.get(UUID(spawned["worker_id"]))
    with structlog.testing.capture_logs() as logs:
        if task is not None:
            async with asyncio.timeout(60):
                await asyncio.gather(task, return_exceptions=True)
    worker = await _settled(ports, parent, spawned["worker_id"])
    assert streaming.is_set() and worker.binding_id is not None  # cancelled after bind
    assert worker.status == "failed"
    assert worker.error_code == "usage_unavailable"
    assert worker_error_text(worker.error_code) == worker_error_text("usage_unavailable")
    assert worker_error_text(worker.error_code) != worker_error_text("dispatch_refused")
    (override,) = [e for e in logs if e["event"] == "fleet.worker_failure_cancel_override"]
    assert (override["original_code"], override["failure_code"]) == (
        "dispatch_refused",
        "usage_unavailable",
    )
