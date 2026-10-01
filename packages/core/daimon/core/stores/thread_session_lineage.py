"""Lifecycle of a task that outlived the session it started in.

A configuration change that cannot be applied in place ends one MA session and
starts another carrying the same work. The rows record that as a chain, never
as an edit: the old row keeps its history and gains `status='superseded'` plus
`replaced_by_id`, and the new row points back through `predecessor_id`. The
caller-scoped lookup in `thread_sessions` only ever returns `'live'`, so a
superseded or retired row is invisible to the turn pipeline the moment it is
marked, without anything being deleted.

`'retired'` is the explicit-fresh-start counterpart: the caller asked to start
over, so there is a predecessor but deliberately no carried work.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime

from daimon.core._models import ThreadSession
from daimon.core.stores.domain import ThreadSessionRow
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

# A task that has been replaced this many times is a loop, not a lineage.
# Bounded so a cycle written by a bug cannot hang a turn.
MAX_LINEAGE_DEPTH = 50


async def mark_superseded(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    replaced_by_id: _uuid.UUID,
) -> None:
    """Hand this row's task to its successor.

    Call only once the successor exists and is usable: until this runs, the old
    row is still the caller's live session and still holds the only copy of the
    work. The order matters — replacement first, supersede second — so a crash
    between them leaves a working session rather than none.

    Any pending uncommitted-work answer is cleared here: it was given for the
    replacement that has now happened, and an answer left behind would govern
    an unrelated replacement later.
    """
    await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id == id)
        .values(status="superseded", replaced_by_id=replaced_by_id, pending_unsaved_work=None)
    )
    await session.flush()


async def link_replacement(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    replaced_by_id: _uuid.UUID,
) -> None:
    """Point a row at the session that took its task over, leaving status alone.

    For the row whose status already says what happened to it — the dead one
    the turn pipeline recreates after an unexpected loss. `mark_superseded` is
    the wrong call there: it would relabel a session that MA deleted or
    archived as a deliberate handover, and the chain would no longer say which
    of the two a lineage walk is looking at.
    """
    await session.execute(
        update(ThreadSession).where(ThreadSession.id == id).values(replaced_by_id=replaced_by_id)
    )
    await session.flush()


async def mark_retired(session: AsyncSession, *, id: _uuid.UUID) -> None:
    """Retire a row abandoned on an explicit fresh start.

    Distinct from `mark_dead` (the MA session is gone) and from
    `mark_superseded` (the work moved on): the session may be perfectly
    healthy, the caller just asked for a clean one.

    Clears any pending uncommitted-work answer for the same reason
    `mark_superseded` does: the row it was given for is no longer live.
    """
    await session.execute(
        update(ThreadSession)
        .where(ThreadSession.id == id)
        .values(status="retired", pending_unsaved_work=None)
    )
    await session.flush()


async def request_fresh_start(
    session: AsyncSession,
    *,
    id: _uuid.UUID,
    at: datetime,
) -> None:
    """Record that the caller asked to start over; nothing is torn down yet.

    The next bind reads this, creates the replacement first and only then
    retires this row, so a fresh start that fails halfway leaves the existing
    work reachable.
    """
    await session.execute(
        update(ThreadSession).where(ThreadSession.id == id).values(fresh_start_requested_at=at)
    )
    await session.flush()


async def clear_fresh_start(session: AsyncSession, *, id: _uuid.UUID) -> None:
    """Clear the request, once it has been honoured or withdrawn."""
    await session.execute(
        update(ThreadSession).where(ThreadSession.id == id).values(fresh_start_requested_at=None)
    )
    await session.flush()


async def get_lineage(session: AsyncSession, *, id: _uuid.UUID) -> list[ThreadSessionRow]:
    """Every session this task has lived in, oldest first, ending at `id`.

    Walks `predecessor_id` one row at a time rather than with a recursive CTE:
    a lineage is a handful of rows, and the loop is where the depth bound and
    the already-seen guard live. Returns an empty list when `id` names no row.
    """
    walked: list[ThreadSessionRow] = []
    seen: set[_uuid.UUID] = set()
    next_id: _uuid.UUID | None = id

    while next_id is not None and next_id not in seen and len(walked) < MAX_LINEAGE_DEPTH:
        seen.add(next_id)
        orm = (
            await session.execute(select(ThreadSession).where(ThreadSession.id == next_id))
        ).scalar_one_or_none()
        if orm is None:
            break
        row = ThreadSessionRow.model_validate(orm)
        walked.append(row)
        next_id = row.predecessor_id

    walked.reverse()
    return walked
