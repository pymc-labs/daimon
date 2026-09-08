"""The per-thread turn driver: send, poll, stream text, terminate, settle.

Split functional-core / imperative-shell. ``read_turn_progress`` is the pure
decision: given a session's status and its events since a turn's boundary,
what new text arrived and is the turn over. ``run_turn`` is the thin
imperative shell around it — the only place that calls the seam, sleeps, or
touches the clock or the database.

**The done rule** (SPEC D-07, §1.3): a turn is finished only when a
``session.status_idle`` event survives the boundary filter, or
``get_my_session`` reports ``terminated``. A bare ``status == "idle"`` is
never trusted — right after a send the session has not started yet and
reads idle — and ``rescheduling`` counts as running. This is the exact bug
the prototype (``spikes/report-host/host/app.py``) hit: it trusted a bare
idle status and ended a follow-up question in three seconds with nothing.

The budget reserved at turn start is reconciled against the seam's real cost
on ANY terminal path — idle, terminated, cancelled-at-deadline, or a bundle
push failure — never only on the clean one (SPEC D-06). Every reconciling
call funnels through the single ``_settle_reservation`` helper below.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from report_host import reports_store, threads_store
from report_host.config import Settings
from report_host.mcp_client import (
    BundleExpiredError,
    SeamClient,
    SeamError,
    SeamUnauthorizedError,
)
from report_host.reports_store import ReportRow
from report_host.threads_store import ThreadRow

# One fixed, reader-facing message for a bundle the host cannot re-push —
# either there is no archive on record, the file is no longer on the volume,
# or it resolves outside `settings.data_dir`. A single constant (not an
# f-string) so a reader is never shown a filesystem path, a slug, or an
# exception string, and so tests can assert on it by identity.
BUNDLE_MISSING_MESSAGE = "this report's files have expired; ask the publisher to re-publish"

_CAP_REACHED_MESSAGE = (
    "This report has reached its spending cap. No further questions can be answered."
)
_UNAUTHORIZED_MESSAGE = (
    "This report's connection to daimon has expired; ask the publisher to re-publish it."
)
_REPORT_MISSING_MESSAGE = "This report could not be found; ask the publisher to re-publish it."
_DEADLINE_MESSAGE = (
    "This question was not answered within the time limit and has been stopped. "
    "It is still billed for what it consumed."
)


@dataclass(frozen=True)
class TurnProgress:
    """One poll's worth of decision: what new text arrived, and is it over."""

    new_texts: tuple[str, ...]
    is_done: bool
    terminal_reason: Literal["idle", "terminated"] | None
    new_event_ids: frozenset[str]


def _agent_message_text(event: dict[str, object]) -> str | None:
    """Fold an ``agent.message`` event's text blocks, the way the seam's own
    transcript reader does (``daimon.adapters.mcp.tools.agent_chat``)."""
    content = event.get("content")
    if not isinstance(content, list):
        return None
    texts: list[str] = []
    for raw_block in cast("list[object]", content):
        if not isinstance(raw_block, dict):
            continue
        block = cast("dict[str, object]", raw_block)
        if block.get("type") == "text" and block.get("text") is not None:
            texts.append(str(block["text"]))
    text = "\n".join(texts)
    return text or None


def read_turn_progress(
    *,
    status: str,
    events: Sequence[dict[str, object]],
    turn_event_id: str,
    seen_event_ids: frozenset[str],
) -> TurnProgress:
    """Pure: decide what is new and whether the turn is over.

    No clock, no I/O. Every event whose id is ``turn_event_id`` (the
    boundary — ``created_at_gte`` is inclusive, so it comes back on every
    poll) or is already in ``seen_event_ids`` is dropped before anything
    else is decided, so a boundary that is itself an idle event, or an
    already-reported answer, can never end or re-report a turn.

    A bare ``status == "idle"`` with no surviving ``session.status_idle``
    event is NOT done — a session that has not started yet reads idle right
    after a send. Neither is ``status == "rescheduling"``. The turn is done
    only when an idle event survives the filter, or ``status == "terminated"``.
    """
    new_texts: list[str] = []
    new_ids: set[str] = set()
    idle_event_survived = False

    for event in events:
        event_id = event.get("id")
        if not isinstance(event_id, str):
            continue
        if event_id == turn_event_id or event_id in seen_event_ids:
            continue
        new_ids.add(event_id)

        event_type = event.get("type")
        if event_type == "agent.message":
            text = _agent_message_text(event)
            if text is not None:
                new_texts.append(text)
        elif event_type == "session.status_idle":
            idle_event_survived = True

    if idle_event_survived:
        is_done, terminal_reason = True, "idle"
    elif status == "terminated":
        is_done, terminal_reason = True, "terminated"
    else:
        is_done, terminal_reason = False, None

    return TurnProgress(
        new_texts=tuple(new_texts),
        is_done=is_done,
        terminal_reason=terminal_reason,
        new_event_ids=frozenset(new_ids),
    )


def _settle_reservation(
    conn: sqlite3.Connection, *, slug: str, reserved: Decimal, actual: Decimal | None
) -> None:
    """Replace a reservation with its real outcome — every terminal path funnels here.

    ``actual=None`` keeps the reservation in place (an unpriced turn);
    ``actual=Decimal("0")`` releases it entirely (no seam turn ever ran, or
    the report was marked unauthorized before one could).
    """
    reports_store.settle_budget(conn, slug=slug, reserved=reserved, actual=actual)


def _resolve_archive_path(*, settings: Settings, report: ReportRow) -> Path | None:
    """Resolve ``report.archive_path`` and confirm it is inside ``data_dir``.

    Returns ``None`` — never raises — when the path is null, is not on the
    volume, or resolves outside ``settings.data_dir``: the corrupted-row case
    (T-21-14-H) a hand-edited database row could otherwise turn into reading
    an arbitrary file. The caller takes the missing-archive branch on
    ``None`` rather than trusting the path.
    """
    if report.archive_path is None:
        return None
    data_dir = settings.data_dir.resolve()
    candidate = Path(report.archive_path).resolve()
    if not candidate.is_relative_to(data_dir):
        return None
    if not candidate.is_file():
        return None
    return candidate


async def run_turn(
    *,
    conn: sqlite3.Connection,
    seam: SeamClient,
    settings: Settings,
    thread: ThreadRow,
    message: str | None,
    now: Callable[[], datetime],
    shutting_down: Callable[[], bool] = lambda: False,
) -> None:
    """Drive one turn to a terminal state, with its reserve settled.

    ``message=None`` re-attaches to a turn the seam is already running after
    a host restart: no send, straight to the poll, using the handle and
    boundary the thread already carries. Collaborators — including the
    clock and the shutdown flag — are injected, never read from a global,
    so a restart's resume sweep and a real reader question call this
    function identically.

    Exception boundary: this runs as a background task, so it is a
    legitimate catch site. Only ``SeamError`` is caught, and it is stored as
    a message the reader can act on; anything else is a bug in the host and
    propagates.
    """
    try:
        report = reports_store.load_report(conn, slug=thread.slug)
        if report is None:
            threads_store.add_message(
                conn,
                thread_id=thread.id,
                role="system",
                text=_REPORT_MISSING_MESSAGE,
                now=now(),
                bundle_sha256=None,
                pdf_revision=None,
            )
            return

        if message is not None:
            reserved = reports_store.reserve_budget(
                conn, slug=report.slug, amount=settings.reserve_usd
            )
            if reserved is None:
                threads_store.add_message(
                    conn,
                    thread_id=thread.id,
                    role="system",
                    text=_CAP_REACHED_MESSAGE,
                    now=now(),
                    bundle_sha256=report.bundle_sha256,
                    pdf_revision=report.current_pdf,
                )
                return

            try:
                if thread.handle is None:
                    started = await seam.start_turn(
                        token=report.seam_token, message=message, bundle=report.bundle_handle
                    )
                else:
                    started = await seam.continue_turn(
                        token=report.seam_token, handle=thread.handle, message=message
                    )
            except BundleExpiredError:
                archive_path = _resolve_archive_path(settings=settings, report=report)
                if archive_path is None:
                    threads_store.add_message(
                        conn,
                        thread_id=thread.id,
                        role="system",
                        text=BUNDLE_MISSING_MESSAGE,
                        now=now(),
                        bundle_sha256=report.bundle_sha256,
                        pdf_revision=report.current_pdf,
                    )
                    _settle_reservation(
                        conn, slug=report.slug, reserved=settings.reserve_usd, actual=Decimal(0)
                    )
                    return

                archive_bytes = archive_path.read_bytes()
                pushed = await seam.push_bundle(
                    token=report.seam_token,
                    archive=archive_bytes,
                    size_bytes=len(archive_bytes),
                )
                assert report.archive_path is not None  # archive_path above is its resolution
                reports_store.save_bundle_reference(
                    conn,
                    slug=report.slug,
                    handle=pushed.handle,
                    sha256=pushed.sha256,
                    expires_at=reports_store.text_to_dt(pushed.expires_at),
                    archive_path=report.archive_path,  # same file: it never moved
                )

                try:
                    started = await seam.start_turn(
                        token=report.seam_token, message=message, bundle=pushed.handle
                    )
                except BundleExpiredError:
                    # The one retry the host performs. A second failure is
                    # surfaced to the reader with the same fixed message;
                    # nothing about the archive is retried again.
                    threads_store.add_message(
                        conn,
                        thread_id=thread.id,
                        role="system",
                        text=BUNDLE_MISSING_MESSAGE,
                        now=now(),
                        bundle_sha256=report.bundle_sha256,
                        pdf_revision=report.current_pdf,
                    )
                    _settle_reservation(
                        conn, slug=report.slug, reserved=settings.reserve_usd, actual=Decimal(0)
                    )
                    return
            except SeamUnauthorizedError:
                reports_store.set_seam_status(conn, slug=report.slug, status="unauthorized")
                threads_store.add_message(
                    conn,
                    thread_id=thread.id,
                    role="system",
                    text=_UNAUTHORIZED_MESSAGE,
                    now=now(),
                    bundle_sha256=report.bundle_sha256,
                    pdf_revision=report.current_pdf,
                )
                _settle_reservation(
                    conn, slug=report.slug, reserved=settings.reserve_usd, actual=Decimal(0)
                )
                return

            handle = started.handle
            turn_event_id = started.turn_event_id
            turn_started_at_text = started.turn_started_at
            threads_store.bind_turn_boundary(
                conn,
                thread_id=thread.id,
                handle=handle,
                turn_event_id=turn_event_id,
                turn_started_at=reports_store.text_to_dt(turn_started_at_text),
            )
        else:
            assert thread.handle is not None, "re-attach requires an already-bound handle"
            assert thread.turn_event_id is not None, "re-attach requires a bound turn boundary"
            assert thread.turn_started_at is not None, "re-attach requires a bound turn boundary"
            handle = thread.handle
            turn_event_id = thread.turn_event_id
            turn_started_at_text = reports_store.dt_to_text(thread.turn_started_at)

        seen_event_ids: frozenset[str] = frozenset()
        cancelled_at_deadline = False
        while True:
            status_result = await seam.get_session(token=report.seam_token, handle=handle)
            events_result = await seam.list_events(
                token=report.seam_token,
                handle=handle,
                created_at_gte=turn_started_at_text,
                types=["agent.message", "session.status_idle"],
            )
            progress = read_turn_progress(
                status=status_result.status,
                events=events_result.items,
                turn_event_id=turn_event_id,
                seen_event_ids=seen_event_ids,
            )
            seen_event_ids = seen_event_ids | progress.new_event_ids
            for text in progress.new_texts:
                threads_store.add_message(
                    conn,
                    thread_id=thread.id,
                    role="assistant",
                    text=text,
                    now=now(),
                    bundle_sha256=report.bundle_sha256,
                    pdf_revision=report.current_pdf,
                )
            if progress.is_done:
                break
            if (
                not cancelled_at_deadline
                and thread.turn_deadline_at is not None
                and now() >= thread.turn_deadline_at
            ):
                await seam.cancel_turn(token=report.seam_token, handle=handle)
                threads_store.add_message(
                    conn,
                    thread_id=thread.id,
                    role="system",
                    text=_DEADLINE_MESSAGE,
                    now=now(),
                    bundle_sha256=report.bundle_sha256,
                    pdf_revision=report.current_pdf,
                )
                cancelled_at_deadline = True
            await asyncio.sleep(settings.poll_interval_seconds)

        cost = await seam.get_turn_cost(
            token=report.seam_token,
            handle=handle,
            turn_started_at=turn_started_at_text,
            turn_event_id=turn_event_id,
        )
        _settle_reservation(
            conn, slug=report.slug, reserved=settings.reserve_usd, actual=cost.cost_usd
        )
    except SeamError as err:
        threads_store.add_message(
            conn,
            thread_id=thread.id,
            role="system",
            text=(
                f"daimon could not answer this ({type(err).__name__}). "
                "Ask again; if it repeats, tell the report's publisher."
            ),
            now=now(),
            bundle_sha256=None,
            pdf_revision=None,
        )
    finally:
        if not shutting_down():
            threads_store.end_turn(conn, thread_id=thread.id, now=now())
