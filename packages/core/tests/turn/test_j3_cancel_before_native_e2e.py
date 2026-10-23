"""J3/J10: a worker the budget watch cancels during its own setup, before the native
session, reads as the watch's reason, not as "the fleet ended".

run_worker records its own pre-native failures. The watch's cancel surfaces there as a
CancelledError from a setup guard, which read as watch_ended whatever the watch said
(SMOKE 56fc2368/5ad12cc3: usage_unavailable cancels landed mid-setup during the burst).
"""

import asyncio

import pytest
from daimon.core.channel_budget import WorkerCancel, cancel_workers
from daimon.core.fleet import worker_error_text

from ..test_fleet_budget import tariffs
from .test_native_session_receipts import native_worker as native_worker
from .test_native_session_receipts import source_read_only as source_read_only
from .test_worker_setup_bound import _ended


async def test_a_watch_cancel_during_setup_reads_as_the_watchs_reason(native_worker, monkeypatch):
    from daimon.core import headless_runner

    request, root, claimed, trace, _ = native_worker
    cancel = WorkerCancel()
    capture = headless_runner.capture_preparation_source

    async def cancelled_mid_setup(*args, **kwargs):
        # The run's budget watch fails closed while this worker is still setting up.
        cancel_workers({claimed.id: cancel}, "usage_unavailable", run_id=claimed.run_id)
        return await capture(*args, **kwargs)

    monkeypatch.setattr(headless_runner, "capture_preparation_source", cancelled_mid_setup)
    async with asyncio.timeout(30):
        with pytest.raises(asyncio.CancelledError):
            await headless_runner.run_worker(
                request,
                parent=root,
                worker=claimed,
                trigger_message="Complete the task",
                prices=tariffs(root),
                cancel=cancel,
            )
    ended = await _ended(request, claimed)
    assert ended.status == "failed" and ended.error_code == "usage_unavailable"
    assert worker_error_text(ended.error_code) != worker_error_text("watch_ended")
    assert not any(call.method == "POST" for call in trace.requests)  # never dispatched
