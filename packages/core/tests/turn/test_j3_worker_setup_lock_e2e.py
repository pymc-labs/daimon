"""J3/J4/J10: a worker whose setup waits on a busy but progressing fleet lock still runs.

SMOKE 56fc2368 J3: during the spawn burst (76 fleet-lock waits in one minute, up to
9.2 s each) opus 80b5d1cd's setup gave up on the tenant fleet lock at 10 s
(worker_setup_busy) and the user saw "ran out of time". A lock that admissions keep
taking and releasing is busy, not stuck: the wait is 20 s, inside the 45 s setup bound.
"""

import asyncio

from daimon.core.headless_runner import WORKER_SETUP_LOCK_TIMEOUT, WORKER_SETUP_TIMEOUT_S
from sqlalchemy import text

from ..test_fleet_budget import tariffs
from .test_native_session_receipts import native_worker as native_worker
from .test_native_session_receipts import source_read_only as source_read_only

#: Longer than the old 10 s wait and the smoke's longest observed one (9.2 s).
HELD_S = 12.0


async def test_a_worker_whose_setup_waits_12s_on_the_fleet_lock_still_runs(native_worker):
    from daimon.core import headless_runner

    assert WORKER_SETUP_LOCK_TIMEOUT == "20s" and WORKER_SETUP_TIMEOUT_S == 45.0
    request, root, claimed, _, _ = native_worker
    held = asyncio.Event()

    async def hold_fleet_lock() -> None:
        # A concurrent admission holds the tenant fleet lock, then lets it go.
        async with request.deps.sessionmaker.begin() as db:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"fleet:{claimed.tenant_id}"},
            )
            held.set()
            await asyncio.sleep(HELD_S)

    holder = asyncio.create_task(hold_fleet_lock())
    try:
        await held.wait()
        async with asyncio.timeout(40):
            result = await headless_runner.run_worker(
                request,
                parent=root,
                worker=claimed,
                trigger_message="Complete the task",
                prices=tariffs(root),
                cancel=asyncio.Event(),
            )
    finally:
        await holder
    assert result.status == "succeeded" and result.turns_used == 1
