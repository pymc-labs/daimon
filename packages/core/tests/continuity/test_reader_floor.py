"""The released completion-aware selection defers safely without its callback."""

from __future__ import annotations

import hashlib
import types
from datetime import timedelta
from pathlib import Path
from typing import Literal, get_args

import pytest
from daimon.core._models import TaskContinuation
from daimon.core.continuity.wakes import WAKE_UNAVAILABLE_DELAY
from daimon.core.stores.domain import TaskContinuationRow
from sqlalchemy import and_, or_, select

from .test_compatible_readers import NOW, seed
from .test_compatible_readers import address as address
from .test_worker_finished import _NOW, _dispatch, _read, _run, _seed_reader_fixture
from .test_worker_finished import pooled as pooled


def _released_reader():
    path = Path(__file__).parent / "fixtures/continuation_compatible.txt"
    source = path.read_bytes()
    assert (
        hashlib.sha256(source).hexdigest()
        == "c06be9a9b3a33d93f743be008dd412c65099e260ce491eca41a5f50ff2afc767"
    )
    reader = types.ModuleType("released_continuation_selection")
    reader.__dict__.update(
        TaskContinuation=TaskContinuation,
        TaskContinuationRow=TaskContinuationRow,
        Literal=Literal,
        get_args=get_args,
        and_=and_,
        or_=or_,
        select=select,
    )
    exec(
        compile("from __future__ import annotations\n" + source.decode(), str(path), "exec"),
        reader.__dict__,
    )
    return reader


@pytest.mark.fresh_schema
@pytest.mark.parametrize("status", ["pending", "claimed"])
async def test_released_selection_includes_completion_before_thread_batching(
    db_session, address, status
):
    reader = _released_reader()
    workers = []
    for i in range(101):
        workers.append(
            await seed(
                db_session,
                address,
                reason="worker_finished",
                thread="thread" if i == 0 else f"worker-{i}",
                status=status,
                created_at=NOW - timedelta(days=1),
                available_at=NOW - timedelta(days=1),
                lease_owner="old-dispatcher" if status == "claimed" else None,
                lease_expires_at=NOW - timedelta(seconds=1) if status == "claimed" else None,
            )
        )
    timer = await seed(db_session, address)
    dispatchable = await reader.list_dispatchable_continuations(
        db_session,
        tenant_id=address["tenant_id"],
        platform="discord",
        thread_id="thread",
        now=NOW,
        max_attempts=5,
    )
    due = await reader.list_due_wake_rows(
        db_session, platform="discord", now=NOW, max_attempts=5, limit=1
    )
    assert [r.idempotency_key for r in dispatchable] == [workers[0], timer]
    assert len(due) == 1 and due[0].idempotency_key in workers


async def test_selected_completion_without_callback_defers_without_input_or_notice(pooled):
    run = await _run(pooled)
    await _seed_reader_fixture(pooled, run)
    before = await _read(pooled, run)
    reader = _released_reader()
    async with pooled() as db:
        due = await reader.list_due_wake_rows(
            db, platform="discord", now=_NOW, max_attempts=5, limit=1
        )
    assert [r.idempotency_key for r in due] == [before.idempotency_key]
    turns = []
    await _dispatch(pooled, run, None, turns)
    after = await _read(pooled, run)
    assert turns == []
    assert after == before.model_copy(
        update={"claimed_at": _NOW, "available_at": _NOW + WAKE_UNAVAILABLE_DELAY}
    )
    async with pooled() as db:
        assert (
            await reader.list_due_wake_rows(
                db, platform="discord", now=_NOW, max_attempts=5, limit=1
            )
            == []
        )
