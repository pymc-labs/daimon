"""An agent-chat session's driver names a person only when they signed in to the hub."""

from __future__ import annotations

import pytest
from daimon.core.agent_chat_requester import counts_as_signed_in, driver_signed_in, record_driver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SESSION = "sesn_driver"


@pytest.mark.parametrize(
    ("signed_in_person", "status", "open_turn", "named"),
    [
        (True, None, False, True),
        (True, "idle", False, True),
        (True, "idle", True, False),
        (True, "running", False, False),
        (True, "rescheduling", False, False),
        (True, "terminated", False, False),
        (False, None, False, False),
        (False, "idle", False, False),
    ],
)
def test_only_a_signed_in_person_on_a_new_or_finished_session_is_named(
    signed_in_person: bool, status: str | None, open_turn: bool, named: bool
) -> None:
    """An idle status is not enough: an earlier send not seen to end may still call."""
    assert (
        counts_as_signed_in(
            signed_in_person=signed_in_person, session_status=status, earlier_turn_open=open_turn
        )
        is named
    )


async def _record(
    factory: async_sessionmaker[AsyncSession], signed_in: bool, *, resuming: bool = False
) -> bool:
    await record_driver(
        factory,
        managed_session_id=SESSION,
        signed_in_person=signed_in,
        session_status="idle",
        earlier_turn_open=False,
        resuming=resuming,
    )
    return await driver_signed_in(factory, managed_session_id=SESSION)


async def test_each_send_replaces_the_driver_and_a_resume_can_only_clear_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert not await driver_signed_in(db_session_factory, managed_session_id=SESSION), (
        "a session with no record names nobody"
    )
    assert await _record(db_session_factory, True), "a signed-in person's send names them"
    assert not await _record(db_session_factory, False), "a key's send after it names nobody"
    assert not await _record(db_session_factory, True, resuming=True), (
        "a person resuming a turn a key sent does not gain it"
    )
    assert await _record(db_session_factory, True), "the person's own next send names them"
    assert await _record(db_session_factory, True, resuming=True), "their resume keeps it"
    assert not await _record(db_session_factory, False, resuming=True), "a key's resume clears it"
