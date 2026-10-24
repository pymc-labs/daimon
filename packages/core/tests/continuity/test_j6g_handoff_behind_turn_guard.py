"""J6g guard (smoke 1250d1c8): a hand-off queued behind its own running turn answers once.

The copy's thread got only "<copy> takes over from your next message here. The
message I'm working on now finishes with me." and never answered: the hand-off's
row was dispatched while the turn that recorded it was still marked active, and it
was settled as skipped after that notice. Now it waits for that turn and then runs,
exactly once, and the person is never told to send another message.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import clear_active_turn, mark_turn_active
from daimon.testing.factories import make_account, make_thread_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_dispatch import _SOON, _THREAD, _dispatch, _queue, _status
from .test_dispatch import caller as caller


async def _active_turn(factory, caller):
    async with factory.begin() as session:
        tenant = await get_tenant(session, caller[0])
        assert tenant is not None
        account = await make_account(session, tenant=tenant)
        live = await make_thread_session(
            session, tenant=tenant, account=account, platform="teams", thread_id=_THREAD
        )
        await mark_turn_active(session, id=live.id, active_turn_message_id="m-1", now=_SOON)
    return live, (caller[0], account.id)


async def test_j6g_the_handoff_answers_once_after_the_turn_in_progress(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live, caller = await _active_turn(db_session_factory, caller)
    monkeypatch.setattr("daimon.core.continuity.dispatch.WAKE_RETRY_DELAY", timedelta(0))
    key = await _queue(db_session_factory, caller, "compare X and Y")
    # Behind its own running turn: no answer yet, and no "next message" notice.
    assert await _dispatch(db_session_factory, caller) == ([], [])
    assert (await _status(db_session_factory, key))[0] == "pending"
    async with db_session_factory.begin() as session:
        await clear_active_turn(session, id=live.id)
    # That turn ended: the copy answers, once.
    assert await _dispatch(db_session_factory, caller) == (["compare X and Y"], [])
    assert await _status(db_session_factory, key) == ("delivered", None)
    assert await _dispatch(db_session_factory, caller) == ([], []), "never a second answer"


async def test_j6g_a_person_who_spoke_meanwhile_gets_one_answer_not_two(
    db_session_factory: async_sessionmaker[AsyncSession],
    caller: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retried hand-off never runs on top of the person's own next message."""
    live, caller = await _active_turn(db_session_factory, caller)
    monkeypatch.setattr("daimon.core.continuity.dispatch.WAKE_RETRY_DELAY", timedelta(0))
    key = await _queue(db_session_factory, caller, "compare X and Y")
    assert await _dispatch(db_session_factory, caller) == ([], [])
    async with db_session_factory.begin() as session:
        await clear_active_turn(session, id=live.id)
    spoke = datetime.now(UTC) + timedelta(minutes=5)
    seeds, _ = await _dispatch(db_session_factory, caller, latest=spoke)
    assert seeds == [], "the person's own message is the one the copy answers"
    assert (await _status(db_session_factory, key))[0] != "pending"
