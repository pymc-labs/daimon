"""The relay names the kind of place a turn's answer goes to in the signed `room` claim."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from daimon.core.audience_facts import RoomFacts
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_identity import RelayBinding, generate_signing_key, routine_destination_key
from daimon.core.stores.audience_facts import StoredRoom, publish_facts
from daimon.core.stores.routines import get_routine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_identity_relay import (
    SERVER,
    _call,
    _chat_session,
    _identity,
    _Person,
    _person,
    _relay,
    _routine,
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


ROUTINE_SERVER = SERVER.model_copy(update={"routine_claims": True})


async def _routine_token(
    factory: async_sessionmaker[AsyncSession],
    person: _Person,
    routine_id: uuid.UUID,
    *,
    owner: str,
    destination: str | None,
) -> str:
    binding = RelayBinding("Agent A", routine_id=routine_id, owner=owner, destination=destination)
    return await _token(factory, person, binding)


async def _claims_of(
    factory: async_sessionmaker[AsyncSession], token: str, server: SignedIdentityServer
) -> dict[str, object]:
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(factory, upstream, key, server) as client:
        await _call(client, token)
    return _identity(upstream.requests[0], key)


async def _routine_claims(
    factory: async_sessionmaker[AsyncSession],
    person: _Person,
    fields: dict[str, object],
    *,
    server: SignedIdentityServer = ROUTINE_SERVER,
    binding_owner: str | None = None,
) -> tuple[uuid.UUID, dict[str, object]]:
    """The claims a run of a new routine with `fields` signs. Its credential names the
    routine's owner unless `binding_owner` is given, and the destination it started with."""
    routine_id = await _routine(factory, person, **fields)
    async with factory() as db:
        row = await get_routine(db, routine_id, tenant_id=person.tenant_id)
    assert row is not None
    owner = binding_owner or str(fields.get("owner", "user-1"))
    token = await _routine_token(
        factory, person, routine_id, owner=owner, destination=routine_destination_key(row)
    )
    return routine_id, await _claims_of(factory, token, server)


@pytest.mark.parametrize(
    ("fields", "channel", "room"),
    [
        ({"destination_kind": "channel", "destination_id": "chan-1"}, "chan-1", "team"),
        ({"destination_kind": "channel", "destination_id": "chan-2"}, "chan-2", "narrow"),
        (
            {"destination_kind": "thread", "destination_id": "thr-1", "channel_id": "chan-1"},
            "thr-1",
            "narrow",
        ),
        ({"destination_kind": "channel", "destination_id": "chan-9"}, "chan-9", "contained"),
    ],
    ids=["team", "narrow", "thread-without-a-fact-is-narrow", "unknown"],
)
async def test_a_routine_run_names_its_routine_the_room_it_posts_to_and_its_kind(
    db_session_factory: async_sessionmaker[AsyncSession],
    fields: dict[str, object],
    channel: str,
    room: str,
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM, "chan-2": NARROW})
    routine_id, claims = await _routine_claims(db_session_factory, person, fields)
    assert (claims["routine"], claims["channel"], claims["room"]) == (
        str(routine_id),
        channel,
        room,
    )
    assert claims["ws"] == "discord:guild-1", "the workspace is named"
    assert not {"sub", "msg"} & set(claims), "a routine names no person and no message"


_CHANNEL = {"destination_kind": "channel", "destination_id": "chan-1"}


@pytest.mark.parametrize(
    ("case", "fields", "owner", "server"),
    [
        ("a DM destination", {"destination_kind": "dm", "owner": "1234"}, None, ROUTINE_SERVER),
        ("no destination", {}, None, ROUTINE_SERVER),
        ("the setting off", _CHANNEL, None, SERVER),
        ("another owner", _CHANNEL, "user-9", ROUTINE_SERVER),
        ("another agent", {**_CHANNEL, "agent_id": "agent_b"}, None, ROUTINE_SERVER),
    ],
    ids=["dm", "no-destination", "setting-off", "another-owner", "another-agent"],
)
async def test_a_routine_run_names_no_routine_or_room_unless_the_run_check_passes(
    db_session_factory: async_sessionmaker[AsyncSession],
    case: str,
    fields: dict[str, object],
    owner: str | None,
    server: SignedIdentityServer,
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    _, claims = await _routine_claims(
        db_session_factory, person, fields, server=server, binding_owner=owner
    )
    assert not {"routine", "channel", "room", "sub", "msg"} & set(claims), (
        f"{case}: the call reads as today"
    )


@pytest.mark.parametrize(
    ("started", "now"),
    [
        ("channel:chan-1:", {"destination_kind": "channel", "destination_id": "chan-2"}),
        ("channel:chan-1:", {"destination_kind": "dm", "destination_id": "1234"}),
        ("channel:chan-1:", {"destination_kind": None, "destination_id": None}),
        (None, {}),
    ],
    ids=["moved-to-another-room", "moved-to-a-dm", "destination-removed", "minted-before"],
)
async def test_a_run_whose_routine_now_posts_elsewhere_signs_no_routine_claim(
    db_session_factory: async_sessionmaker[AsyncSession],
    started: str | None,
    now: dict[str, object],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM, "chan-2": TEAM})
    routine_id = await _routine(db_session_factory, person, **_CHANNEL, owner="1234")
    if now:
        async with db_session_factory() as db, db.begin():
            await db.execute(
                text(
                    "UPDATE routines SET destination_kind = :kind, destination_id = :id"
                    " WHERE id = :routine"
                ),
                {
                    "kind": now.get("destination_kind"),
                    "id": now.get("destination_id"),
                    "routine": routine_id,
                },
            )
    token = await _routine_token(
        db_session_factory, person, routine_id, owner="1234", destination=started
    )
    claims = await _claims_of(db_session_factory, token, ROUTINE_SERVER)
    assert not {"routine", "channel", "room"} & set(claims), (
        "the run signs only the room it started for, and only with a credential that names it"
    )


async def test_a_thread_opened_for_the_answer_signs_the_message_channel_and_the_threads_kind(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A mention in chan-1 answered in a new public thread-1: `channel` stays chan-1, where
    the server fetches the message, and `room` judges thread-1, which reads as chan-1."""
    person = await _person(db_session_factory)
    token, _ = await _chat_session(db_session_factory, person, "thread-1", message="m-1")
    async with db_session_factory() as db, db.begin():
        await db.execute(
            text(
                "UPDATE turn_origins SET platform_channel_id = 'chan-1', public_thread = true"
                " WHERE thread_id = 'thread-1'"
            )
        )
    for facts, room in ((TEAM, "team"), (NARROW, "narrow")):
        await _facts(db_session_factory, person, {"chan-1": facts})
        claims = await _claims_of(db_session_factory, token, SERVER)
        assert (claims["channel"], claims["msg"], claims["room"]) == ("chan-1", "m-1", room), (
            "the message's channel, with the kind of the thread the answer goes to"
        )


async def test_a_routine_reads_a_public_thread_with_no_fact_as_narrow_where_a_turn_reads_its_parent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deliberate difference: a turn knows its thread is public from Discord, a routine
    does not record it, so with no fact of the thread's own the routine reads narrow. It
    only ever reads less (`daimon.core.signed_room.routine_room`)."""
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token, _ = await _chat_session(db_session_factory, person, "thr-1")
    async with db_session_factory() as db, db.begin():
        await db.execute(text("UPDATE turn_origins SET public_thread = true"))
    assert await _room(db_session_factory, token) == ["team"], "a turn takes its parent's"
    _, claims = await _routine_claims(
        db_session_factory,
        person,
        {"destination_kind": "thread", "destination_id": "thr-1", "channel_id": "chan-1"},
    )
    assert (claims["channel"], claims["room"]) == ("thr-1", "narrow"), "a routine reads narrow"
