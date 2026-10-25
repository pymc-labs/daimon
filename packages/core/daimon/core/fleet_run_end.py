"""Tell a thread its fleet ended when no turn rendered the run's end there.

A run that ends owes its thread a terminal card (`fleet_run.end_notice`).
The root turn, a wake turn or a Stop press usually renders it and settles that
at once. A run the deadline sweep ended, whose wake was refused, or that never
parked a wake has nobody to do so: without this its card stays at "running".
The adapter polls those runs, settles the card and posts one plain line.

Delivery is at least once: a crash between the post and its settle posts again
after the backoff. One run's failure never holds up the others.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from daimon.core.stores import fleet
from daimon.core.stores.fleet import EndNotice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: What the adapter did: "shown" (the card and notice are out), "retry" (a
#: transient failure, or a cost still being counted; the claim's backoff applies),
#: "give_up" (it can never be shown there: the thread is gone, locked or forbidden)
#: or "cost_owed" (the end is out, but a worker's cost is still being counted: the
#: card's cost is edited in once it settles).
type EndOutcome = Literal["shown", "retry", "give_up", "cost_owed"]
PostRunEnd = Callable[[EndNotice], Awaitable[EndOutcome]]
_log = structlog.get_logger(__name__)


async def poll_run_end_notices_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    platform: str,
    post: PostRunEnd,
) -> int:
    """Post every due notice, outside the claim's row locks; returns how many showed."""
    shown = 0
    for notice in await fleet.claim_end_notices(sessionmaker, platform=platform):
        run_id = str(notice.run.id)
        try:
            outcome = await post(notice)
        except Exception:
            _log.exception("fleet.end_notice_failed", run_id=run_id, attempts=notice.attempts)
            continue
        if outcome == "retry":
            _log.warning("fleet.end_notice_retry", run_id=run_id, attempts=notice.attempts)
            continue
        if outcome == "give_up":
            _log.error("fleet.end_notice_given_up", run_id=run_id, reason="unpostable")
        await fleet.settle_end_notice(
            sessionmaker,
            tenant_id=notice.run.tenant_id,
            run_id=notice.run.id,
            outcome="given_up" if outcome == "give_up" else outcome,
        )
        shown += outcome in ("shown", "cost_owed")
    return shown


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
