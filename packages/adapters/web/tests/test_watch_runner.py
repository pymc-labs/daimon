"""A fired watch on the web: an unattended read-and-report turn in its owner's chat that
hands a busy chat or full queue back to the wake queue, showing nothing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from daimon.adapters.web.watch_runner import WebWatchRunner
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import TaskContinuationRow, WebThreadRow
from daimon.core.turn.errors import SessionBusyError, TurnNotStarted
from daimon.core.turn.state import TurnState
from daimon.core.web_identity import place_of, thread_key
from daimon.testing.turn_router import build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import Workspace, build_turns, funded_workspace, new_thread, one_session

SEED = "<untrusted_content>a vote</untrusted_content>"


def _wake(ws: Workspace, thread: WebThreadRow) -> TaskContinuationRow:
    return TaskContinuationRow(
        id=uuid.uuid4(),
        tenant_id=ws.tenant_id,
        platform="web",
        thread_id=thread_key(thread.id),
        parent_channel_id=place_of(ws.person.id),
        requester_account_id=ws.person.account_id,
        requester_external_user_id=str(ws.person.id),
        target_ma_agent_id=thread.agent_id,
        target_name=thread.agent_name,
        requested_work=None,
        reason="watch_fired",
        status="claimed",
        skip_reason=None,
        idempotency_key=uuid.uuid4(),
        created_at=datetime.now(UTC),
        claimed_at=None,
        delivered_at=None,
    )


async def _setup(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[WebWatchRunner, Workspace, WebThreadRow]:
    ws = await funded_workspace(factory)
    runner = WebWatchRunner(build_turns(factory, build_turn_router(str(ws.tenant_id)), tmp_path))
    thread, _ = await new_thread(factory, ws)
    return runner, ws, thread


async def test_a_full_queue_or_busy_chat_hands_the_wake_back_unseen(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, ws, thread = await _setup(db_session_factory, tmp_path)
    with (
        patch.object(runner.turns.queue, "admit", return_value=None),
        pytest.raises(TurnNotStarted) as shed,
    ):
        await runner.run(_wake(ws, thread), SEED)
    assert shed.value.capacity, "a full queue is retried"
    runner.turns.streams.open(thread.id, uuid.uuid4())
    with pytest.raises(SessionBusyError):
        await runner.run(_wake(ws, thread), SEED)
    async with db_session_factory() as db:
        messages = await chat_store.list_messages(db, thread_id=thread.id)
    assert [m.role for m in messages] == ["user"], "nothing shows in the chat"


async def test_a_watch_turn_runs_unattended_with_its_evidence_as_the_cue(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, ws, thread = await _setup(db_session_factory, tmp_path)
    seen: dict[str, Any] = {}

    async def run(*_args: object, **kw: Any) -> SimpleNamespace:
        seen.update(kw)
        return SimpleNamespace(state=TurnState(), outputs=None, ma_session_id="s")

    wake = _wake(ws, thread)
    with one_session(), patch("daimon.adapters.web.turns.run_prepared_turn", run):
        await runner.run(wake, SEED)
    assert (seen["watch_row"], seen["attended"], seen["request_text"]) == (wake, False, None)
    assert seen["user_message"].endswith(SEED), "the evidence arrives as given, wrapped"


async def test_a_new_chat_report_makes_one_chat_and_reuses_it_on_retry(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runner, ws, thread = await _setup(db_session_factory, tmp_path)
    ran: list[str] = []

    async def run(row: TaskContinuationRow, _seed: str) -> None:
        ran.append(row.thread_id)

    watch = SimpleNamespace(delivery="new_thread", instructions="Tally  the votes.")
    firing = SimpleNamespace(watch_id=uuid.uuid4(), delivery_thread_id=None)

    async def recorded(_db: object, _key: object, thread_id: str) -> str:
        firing.delivery_thread_id = thread_id
        return thread_id

    wake = _wake(ws, thread).model_copy(update={"reason": "watch_report"})
    with (
        patch.object(runner, "run", run),
        patch("daimon.adapters.web.watch_runner.get_watch", return_value=watch),
        patch("daimon.adapters.web.watch_runner.get_firing", return_value=firing),
        patch("daimon.adapters.web.watch_runner.record_delivery_thread", recorded),
    ):
        await runner.run_report(wake, SEED)
        await runner.run_report(wake, SEED)
    assert ran[0] == ran[1] != thread_key(thread.id)
    async with db_session_factory() as db:
        titles = [t.title for t in await chat_store.list_threads(db, person_id=ws.person.id)]
    assert sorted(titles) == ["New chat", "Watch report: Tally the votes."]
