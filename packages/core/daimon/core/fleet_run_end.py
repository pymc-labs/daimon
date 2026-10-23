"""Tell a thread its fleet ended when no wake delivered the result.

The deadline sweep ends a run in whichever process runs it, and withdraws the
run's wakes as "run_expired". Nothing is then owed to the root, so without this
the run's card stays at "running" and nobody hears the fleet ended. The adapter
polls those withdrawn wakes, settles the card and posts one plain notice per run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from daimon.core.fleet import RUN_TRANSITIONS, FleetConflict, FleetRun, FleetWorker
from daimon.core.stores import fleet, task_continuations
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: The adapter hook: settle the run's card and post the notice. True when done
#: (or the thread can never be reached), False to retry on a later poll.
PostRunEnd = Callable[[FleetRun, tuple[FleetWorker, ...]], Awaitable[bool]]
_log = structlog.get_logger(__name__)


async def poll_run_end_notices_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: PostRunEnd,
    limit: int = 20,
) -> int:
    """Claim, post and settle expired runs while row locks keep the post single."""
    posted = 0
    for _ in range(limit):
        async with sessionmaker.begin() as session:
            claim = await task_continuations.claim_run_end_notice(session, platform=platform)
            if claim is None:
                break
            tenant_id, run_id, thread_id, ids = claim
            try:
                run, workers = await fleet.run_status(session, tenant_id=tenant_id, run_id=run_id)
            except FleetConflict:
                run, workers = None, ()
            if (
                run is not None
                and run.platform == platform
                and run.thread_id == thread_id
                and run.status not in RUN_TRANSITIONS
            ):
                if not await post(run, workers):
                    break
                posted += 1
            else:
                _log.info("fleet.run_end_notice_skipped", run_id=str(run_id))
            await task_continuations.mark_run_end_notice_shown(session, ids=ids)
    return posted


async def run_run_end_poller(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: PostRunEnd,
    should_stop: Callable[[], bool],
    interval_s: float = 15.0,
) -> None:
    while not should_stop():
        try:
            await poll_run_end_notices_once(sessionmaker, platform=platform, post=post)
        except Exception:
            _log.exception("fleet.run_end_poll_failed", platform=platform)
        await asyncio.sleep(interval_s)
