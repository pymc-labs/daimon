"""Real-DB behavior tests for the pending_file_deletes store. Phase 51 (D-07)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from daimon.core.stores.domain import PendingFileDeleteRow
from daimon.core.stores.pending_file_deletes import (
    delete_pending_file_delete,
    enqueue_pending_file_delete,
    list_due_pending_file_deletes,
)
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_enqueue_then_list_due_returns_row_when_now_past_delete_after(
    db_session: AsyncSession,
) -> None:
    """A pending delete whose delete_after is in the past is returned by list_due."""
    file_id = f"file_{uuid.uuid4().hex}"
    delete_after = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)

    await enqueue_pending_file_delete(db_session, file_id=file_id, delete_after=delete_after)

    due = await list_due_pending_file_deletes(db_session, now=dt.datetime.now(dt.UTC))
    assert [r.file_id for r in due] == [file_id], "due query should return the enqueued row"
    assert isinstance(due[0], PendingFileDeleteRow), "store should return Pydantic, not ORM"


@pytest.mark.asyncio
async def test_enqueue_twice_same_file_id_updates_delete_after_without_duplicate(
    db_session: AsyncSession,
) -> None:
    """Re-enqueue on the same file_id upserts delete_after — no duplicate row."""
    file_id = f"file_{uuid.uuid4().hex}"
    first = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
    second = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)

    await enqueue_pending_file_delete(db_session, file_id=file_id, delete_after=first)
    await enqueue_pending_file_delete(db_session, file_id=file_id, delete_after=second)

    due = await list_due_pending_file_deletes(db_session, now=dt.datetime.now(dt.UTC))
    rows = [r for r in due if r.file_id == file_id]
    assert len(rows) == 1, "upsert must not create a duplicate row for the same file_id"
    assert rows[0].delete_after == second, "second enqueue should update delete_after"


@pytest.mark.asyncio
async def test_list_due_excludes_rows_with_future_delete_after(
    db_session: AsyncSession,
) -> None:
    """A pending delete whose delete_after is in the future is not yet due."""
    file_id = f"file_{uuid.uuid4().hex}"
    future = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)

    await enqueue_pending_file_delete(db_session, file_id=file_id, delete_after=future)

    due = await list_due_pending_file_deletes(db_session, now=dt.datetime.now(dt.UTC))
    assert file_id not in [r.file_id for r in due], "future delete_after should not be due"


@pytest.mark.asyncio
async def test_list_due_orders_by_delete_after_ascending(
    db_session: AsyncSession,
) -> None:
    """Due rows come back oldest delete_after first."""
    older_id = f"file_{uuid.uuid4().hex}"
    newer_id = f"file_{uuid.uuid4().hex}"
    older = dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)
    newer = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)

    # Insert newer first to prove ordering is by delete_after, not insertion order.
    await enqueue_pending_file_delete(db_session, file_id=newer_id, delete_after=newer)
    await enqueue_pending_file_delete(db_session, file_id=older_id, delete_after=older)

    due = await list_due_pending_file_deletes(db_session, now=dt.datetime.now(dt.UTC))
    ordered = [r.file_id for r in due if r.file_id in {older_id, newer_id}]
    assert ordered == [older_id, newer_id], "due rows must be ordered by delete_after ascending"


@pytest.mark.asyncio
async def test_delete_removes_row_and_is_idempotent_when_absent(
    db_session: AsyncSession,
) -> None:
    """delete removes the row; calling it on an absent file_id does not raise."""
    file_id = f"file_{uuid.uuid4().hex}"
    delete_after = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)

    await enqueue_pending_file_delete(db_session, file_id=file_id, delete_after=delete_after)
    await delete_pending_file_delete(db_session, file_id=file_id)

    due = await list_due_pending_file_deletes(db_session, now=dt.datetime.now(dt.UTC))
    assert file_id not in [r.file_id for r in due], "row should be gone after delete"

    # Idempotent: deleting an absent file_id must not raise.
    await delete_pending_file_delete(db_session, file_id=f"file_{uuid.uuid4().hex}")
