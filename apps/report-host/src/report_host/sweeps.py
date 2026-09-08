"""Restart-resume, deadline-cancel, idle-archive and recipient-expiry sweeps.

Every function takes its collaborators explicitly — a connection, the seam
client, the settings, an injected clock, and (for the resume pass) the
scheduler used to launch a re-attach and the turn driver itself. No
module-level state, no clock reached for from inside.

The prototype (``spikes/report-host/host/app.py``) hit this restart problem
head-on: a deploy mid-turn surfaced a raw ``'NoneType' object is not
subscriptable`` because its poll task died while the seam turn kept running.
These four sweeps are the host's cleanup: re-attach to what survived a
restart, cancel what timed out while the host was down, archive what has
sat idle too long, and prune what has expired — one bad row must never abort
a whole pass (T-21-18-G), so every loop below catches per item, logs, and
continues.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal

from report_host import reports_store, threads_store, turns
from report_host.config import Settings
from report_host.mcp_client import SeamClient

log = logging.getLogger(__name__)

_RESTART_ASK_AGAIN_MESSAGE = (
    "The report host restarted before daimon received this question. Please ask it again."
)
_DEADLINE_CANCELLED_MESSAGE = (
    "This question was not answered within the time limit and has been stopped. "
    "It is still billed for what it consumed."
)


async def resume_running_threads(
    *,
    conn: sqlite3.Connection,
    seam: SeamClient,
    settings: Settings,
    now: Callable[[], datetime],
    schedule: Callable[[Awaitable[None]], object],
    run_turn: Callable[..., Awaitable[None]] = turns.run_turn,
) -> int:
    """Re-attach to every thread the host left ``running`` across a restart.

    A thread with no bound ``handle`` never reached the seam — the question
    was lost before it was ever sent. Tell the reader to ask again, release
    the reservation (no seam turn ever ran, so there is nothing to bill),
    and end the turn; never schedule anything for it. A thread with a
    handle is a turn the seam is still running server-side: schedule
    ``run_turn(..., message=None)`` so the poller resumes exactly where it
    left off, distinguishing "lost" from "still running" (T-21-18-A,
    T-21-18-C). Returns how many re-attaches were scheduled.
    """
    resumed = 0
    for thread in threads_store.list_running_threads(conn):
        try:
            if thread.handle is None:
                threads_store.add_message(
                    conn,
                    thread_id=thread.id,
                    role="system",
                    text=_RESTART_ASK_AGAIN_MESSAGE,
                    now=now(),
                    bundle_sha256=None,
                    pdf_revision=None,
                )
                if thread.reserved_usd is not None:
                    reports_store.settle_budget(
                        conn, slug=thread.slug, reserved=thread.reserved_usd, actual=Decimal(0)
                    )
                threads_store.end_turn(conn, thread_id=thread.id, now=now())
                continue
            schedule(
                run_turn(
                    conn=conn,
                    seam=seam,
                    settings=settings,
                    thread=thread,
                    message=None,
                    now=now,
                )
            )
            resumed += 1
        except Exception:
            log.exception("resume failed for thread=%s; continuing", thread.id)
            continue
    return resumed


async def cancel_expired_turns(
    *, conn: sqlite3.Connection, seam: SeamClient, settings: Settings, now: Callable[[], datetime]
) -> int:
    """Cancel a running turn whose deadline passed while the host was down.

    Left unwatched, such a turn would run to completion with no poller
    watching it settle (T-21-18-A — this is the after-a-restart case; a live
    host's own ``run_turn`` already cancels at its deadline itself). Calls
    ``cancel_turn`` once and stores the billing-disclosure message, then
    leaves the thread ``running`` — the poller (re-attached by
    ``resume_running_threads``) reaches the seam's own terminal state and
    settles the reservation itself; this sweep never ends a turn. Returns
    how many were cancelled.
    """
    cancelled = 0
    current = now()
    for thread in threads_store.list_running_threads(conn):
        try:
            if thread.handle is None or thread.turn_deadline_at is None:
                continue
            if thread.turn_deadline_at > current:
                continue
            report = reports_store.load_report(conn, slug=thread.slug)
            if report is None:
                continue
            await seam.cancel_turn(token=report.seam_token, handle=thread.handle)
            threads_store.add_message(
                conn,
                thread_id=thread.id,
                role="system",
                text=_DEADLINE_CANCELLED_MESSAGE,
                now=now(),
                bundle_sha256=report.bundle_sha256,
                pdf_revision=report.current_pdf,
            )
            cancelled += 1
        except Exception:
            log.exception("deadline cancel failed for thread=%s; continuing", thread.id)
            continue
    return cancelled


async def archive_idle_threads(
    *, conn: sqlite3.Connection, seam: SeamClient, settings: Settings, now: Callable[[], datetime]
) -> int:
    """Archive, upstream then locally, every thread idle past the configured window.

    The seam call must succeed before the local archive happens (T-21-18-B):
    a local-only archive would hide a session that is still live upstream
    and never reclaimed by the seam's own usage sweep. A seam failure on one
    thread must not stop the others. A thread that never bound a handle
    (idle without ever running a turn) has nothing to archive upstream and
    is archived locally outright. Returns how many were archived.
    """
    cutoff = now() - timedelta(hours=settings.thread_idle_archive_hours)
    archived = 0
    for thread in threads_store.list_threads_idle_since(conn, cutoff=cutoff):
        try:
            if thread.handle is not None:
                report = reports_store.load_report(conn, slug=thread.slug)
                if report is None:
                    continue
                await seam.archive_session(token=report.seam_token, handle=thread.handle)
            threads_store.archive_thread(conn, thread_id=thread.id, now=now())
            archived += 1
        except Exception:
            log.exception("idle archive failed for thread=%s; continuing", thread.id)
            continue
    return archived


def prune_expired_recipients(*, conn: sqlite3.Connection, now: Callable[[], datetime]) -> int:
    """Delete recipient rows past their expiry.

    Belt and braces: expiry is already enforced at the reader route
    (``reports_store.load_recipient``'s own ``expires_at`` check), but
    pruning keeps the table from growing forever and keeps a link dead even
    if that route-level check were ever relaxed (T-21-18-E). Returns how
    many rows were removed.
    """
    return reports_store.prune_expired_recipients(conn, now=now())
