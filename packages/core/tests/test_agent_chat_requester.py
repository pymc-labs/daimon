"""An agent-chat session's driver names a person only when they signed in to the hub."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.agent_chat_requester import (
    UNREAD,
    EarlierTurn,
    counts_as_signed_in,
    driver_signed_in,
    record_driver,
)
from daimon.core.stores import session_settle_state
from daimon.core.stores.session_settle_state import AgentChatSession, SessionProvenance
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: A send whose session carries no provenance stamp.
NO_PROVENANCE = SessionProvenance(grant=None, channel_id=None, account_id=None)

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


async def _sends(factory: async_sessionmaker[AsyncSession], session_id: str) -> int | None:
    async with factory() as db:
        sends = await session_settle_state.count_agent_chat_sends(db, managed_session_id=session_id)
    return sends.begun if sends is not None else None


async def _record(
    factory: async_sessionmaker[AsyncSession], signed_in: bool, *, resuming: bool = False
) -> bool:
    """A send or resume on the existing session that read no earlier turn open."""
    await record_driver(
        factory,
        managed_session_id=SESSION,
        provenance=NO_PROVENANCE,
        signed_in_person=signed_in,
        session_status="idle",
        earlier=EarlierTurn(open=False, sends=await _sends(factory, SESSION)),
        resuming=resuming,
    )
    return await driver_signed_in(factory, managed_session_id=SESSION)


async def test_each_send_replaces_the_driver_and_a_resume_can_only_clear_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert not await driver_signed_in(db_session_factory, managed_session_id=SESSION), (
        "a session with no record names nobody"
    )
    assert await _start(db_session_factory, SESSION), "a signed-in person's start names them"
    assert await _record(db_session_factory, True), "and so does their next send"
    assert not await _record(db_session_factory, False), "a key's send after it names nobody"
    assert not await _record(db_session_factory, True, resuming=True), (
        "a person resuming a turn a key sent does not gain it"
    )
    assert await _record(db_session_factory, True), "the person's own next send names them"
    assert await _record(db_session_factory, True, resuming=True), "their resume keeps it"
    assert not await _record(db_session_factory, False, resuming=True), "a key's resume clears it"


async def _start(factory: async_sessionmaker[AsyncSession], session_id: str) -> bool:
    """A signed-in person's start that read no earlier send."""
    await record_driver(
        factory,
        managed_session_id=session_id,
        provenance=NO_PROVENANCE,
        signed_in_person=True,
        session_status=None,
        earlier=EarlierTurn(open=False),
        new_session=True,
    )
    return await driver_signed_in(factory, managed_session_id=session_id)


async def test_a_start_names_its_person_only_if_its_write_created_the_row(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The start's decision and write are one statement, so a key's send landing
    between the start's read and its write still names nobody."""
    assert await _start(db_session_factory, "sesn_fresh"), "nothing was there"
    await record_driver(
        db_session_factory,
        managed_session_id=SESSION,
        provenance=NO_PROVENANCE,
        signed_in_person=False,
        session_status=None,
        earlier=UNREAD,
    )
    assert not await _start(db_session_factory, SESSION), "another caller wrote first"


async def test_a_send_names_its_person_only_if_no_send_began_since_its_read(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """On an existing session the write checks the count the decision read, in the
    same statement, so a key's send landing between the read and the write still
    names nobody."""
    assert await _start(db_session_factory, SESSION)
    read = EarlierTurn(open=False, sends=await _sends(db_session_factory, SESSION))
    await record_driver(  # a key's send, between the person's read and write
        db_session_factory,
        managed_session_id=SESSION,
        provenance=NO_PROVENANCE,
        signed_in_person=False,
        session_status="idle",
        earlier=UNREAD,
    )
    await record_driver(
        db_session_factory,
        managed_session_id=SESSION,
        provenance=NO_PROVENANCE,
        signed_in_person=True,
        session_status="idle",
        earlier=read,
    )
    assert not await driver_signed_in(db_session_factory, managed_session_id=SESSION)


async def test_each_send_is_numbered_and_a_resume_is_not(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def send(*, resuming: bool = False, person: bool = False) -> int | None:
        return await record_driver(
            db_session_factory,
            managed_session_id=SESSION,
            provenance=NO_PROVENANCE,
            signed_in_person=person,
            session_status="idle",
            earlier=UNREAD,
            resuming=resuming,
        )

    assert await send() == 1
    assert await send(person=True) == 2
    assert await send(resuming=True) is None
    assert await send(resuming=True, person=True) is None
    assert await send() == 3, "a resume starts no new turn, so it is not counted"


async def test_the_store_names_nobody_on_a_resume(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only a send can name its driver, even if a caller asks the store to on a resume."""
    assert await _start(db_session_factory, SESSION)
    async with db_session_factory.begin() as db:
        await session_settle_state.record_agent_chat_driver(
            db,
            managed_session_id=SESSION,
            provenance=NO_PROVENANCE,
            signed_in=True,
            send=False,
            sends_read=await _sends(db_session_factory, SESSION),
        )
    assert not await driver_signed_in(db_session_factory, managed_session_id=SESSION)


async def _decided(
    factory: async_sessionmaker[AsyncSession], session_id: str
) -> tuple[int | None, int | None, str | None, bool]:
    """What the driver record decides from: the sends begun and recorded, the recorded turn
    and the driver. No row reads as an empty one."""
    async with factory() as db:
        sends = await session_settle_state.count_agent_chat_sends(db, managed_session_id=session_id)
        signed_in = await session_settle_state.is_agent_chat_signed_in(
            db, managed_session_id=session_id
        )
    if sends is None:
        return None, None, None, signed_in
    return sends.begun, sends.recorded, sends.turn_event_id, signed_in


@pytest.mark.parametrize("before", ["no_record", "signed_in_send"])
async def test_a_signed_in_resume_records_only_where_the_session_came_from(
    db_session_factory: async_sessionmaker[AsyncSession], before: str
) -> None:
    """A session made before provenance was kept is found after a person's resume too,
    and the resume changes nothing the driver record decides."""
    account = uuid.uuid4()
    if before == "signed_in_send":
        assert await _start(db_session_factory, SESSION)
        async with db_session_factory.begin() as db:
            await session_settle_state.record_unattended_turn(
                db, managed_session_id=SESSION, turn_event_id="sevt_1", rules_channel_id=None,
                send=1,
            )  # fmt: skip
    decided = await _decided(db_session_factory, SESSION)
    await record_driver(
        db_session_factory,
        managed_session_id=SESSION,
        provenance=SessionProvenance(grant="grant-r", channel_id="chan-r", account_id=account),
        signed_in_person=True,
        session_status="idle",
        earlier=UNREAD,
        resuming=True,
    )
    async with db_session_factory() as db:
        found = await session_settle_state.find_agent_chat_session(
            db, grant="grant-r", account_id=account
        )
    assert found == AgentChatSession(managed_session_id=SESSION, channel_id="chan-r")
    assert await _decided(db_session_factory, SESSION) == decided


async def test_a_grant_another_session_holds_refuses_the_write(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Each session's grant is a fresh uuid4, so a shared one is a bug: the write raises, so
    the send it precedes is not made, and the first session keeps its grant."""
    account = uuid.uuid4()
    shared = SessionProvenance(grant="grant-shared", channel_id=None, account_id=account)

    async def recorded(session_id: str, *, person: bool = False) -> None:
        await record_driver(
            db_session_factory, managed_session_id=session_id, provenance=shared,
            signed_in_person=person, session_status="idle", earlier=UNREAD, resuming=person,
        )  # fmt: skip

    await recorded("sesn-a")
    for person in (False, True):
        with pytest.raises(IntegrityError):
            await recorded("sesn-b", person=person)
    async with db_session_factory() as db:
        found = await session_settle_state.find_agent_chat_session(
            db, grant="grant-shared", account_id=account
        )
    assert found == AgentChatSession(managed_session_id="sesn-a", channel_id=None)
