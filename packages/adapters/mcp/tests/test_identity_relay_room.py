"""The relay names the kind of place a turn's answer goes to in the signed `room` claim."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime as dt

import pytest
from daimon.core.audience_facts import RoomFacts
from daimon.core.mcp_identity import RelayBinding, generate_signing_key
from daimon.core.stores.audience_facts import StoredRoom, publish_facts
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_identity_relay import (
    _call,
    _chat_session,
    _identity,
    _Person,
    _person,
    _relay,
    _token,
    _Upstream,
)

TEAM = RoomFacts(contained=False, narrow=False)
NARROW = RoomFacts(contained=False, narrow=True)
CONTAINED = RoomFacts(contained=True, narrow=False)


async def _facts(
    factory: async_sessionmaker[AsyncSession],
    person: _Person,
    rooms: dict[str, RoomFacts | StoredRoom],
    *,
    fresh: dt.timedelta = dt.timedelta(hours=1),
) -> None:
    now = dt.datetime.now(dt.UTC)
    async with factory() as db, db.begin():
        await publish_facts(
            db,
            tenant_id=person.tenant_id,
            rooms={k: v if isinstance(v, StoredRoom) else StoredRoom(v) for k, v in rooms.items()},
            now=now,
            fresh_until=now + fresh,
        )


async def _room(
    factory: async_sessionmaker[AsyncSession], token: str, *, calls: int = 1
) -> list[object]:
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(factory, upstream, key) as client:
        for _ in range(calls):
            await _call(client, token)
    return [_identity(request, key).get("room") for request in upstream.requests]


@pytest.mark.parametrize(
    ("facts", "room"),
    [
        (TEAM, "team"),
        (NARROW, "narrow"),
        (CONTAINED, "contained"),
        (RoomFacts(contained=True, narrow=True), "contained"),
    ],
)
async def test_a_channel_turn_names_its_rooms_kind_contained_first(
    db_session_factory: async_sessionmaker[AsyncSession], facts: RoomFacts, room: str
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "chan-1")
    await _facts(db_session_factory, person, {"chan-1": facts})
    assert await _room(db_session_factory, token) == [room], (
        "the room's facts decide its kind, whatever the audience mode; contained wins"
    )


@pytest.mark.parametrize(
    ("rooms", "fresh"),
    [({"chan-2": TEAM}, dt.timedelta(hours=1)), ({"chan-1": TEAM}, -dt.timedelta(seconds=1))],
    ids=["no-fact", "lapsed"],
)
async def test_a_room_with_no_fact_or_a_lapsed_one_is_contained(
    db_session_factory: async_sessionmaker[AsyncSession],
    rooms: dict[str, RoomFacts | StoredRoom],
    fresh: dt.timedelta,
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "chan-1")
    await _facts(db_session_factory, person, rooms, fresh=fresh)
    assert await _room(db_session_factory, token) == ["contained"], "unknown counts as contained"


@pytest.mark.parametrize(("public", "room"), [(True, "team"), (False, "narrow")])
async def test_a_new_thread_takes_its_parents_kind_only_when_discord_says_it_is_public(
    db_session_factory: async_sessionmaker[AsyncSession], public: bool, room: str
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1")
    async with db_session_factory() as db, db.begin():
        await db.execute(
            text("UPDATE turn_origins SET public_thread = :public WHERE thread_id = 'thread-1'"),
            {"public": public},
        )
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    assert await _room(db_session_factory, token) == [room], (
        "a public thread with no fact yet reads as its parent; any other thread is narrow"
    )


async def test_a_dm_is_named_by_its_channel_type_before_any_room_fact(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "dm:user-1", dm=True)
    async with db_session_factory() as db, db.begin():
        await db.execute(
            text("UPDATE turn_origins SET platform_channel_id = 'dm-channel-1'"),
        )
    assert await _room(db_session_factory, token) == ["dm"], "no fact is needed for a DM"


async def test_a_room_that_turns_narrow_between_two_calls_carries_narrow_on_the_next(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "chan-1")
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    assert await _room(db_session_factory, token) == ["team"], "an open room"
    await _facts(db_session_factory, person, {"chan-1": NARROW})
    assert await _room(db_session_factory, token) == ["narrow"], "read again for each call"


async def test_a_call_with_no_channel_names_no_room(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    headless = await _token(db_session_factory, person, RelayBinding("Agent A"))
    stale, _ = await _chat_session(db_session_factory, person, "chan-1", turn=False)
    for token in (headless, stale):
        assert await _room(db_session_factory, token) == [None], (
            "a room is named only with the channel the server reads"
        )
