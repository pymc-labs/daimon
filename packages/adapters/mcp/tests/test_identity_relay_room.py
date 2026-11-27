"""The relay names the kind of place a turn's answer goes to in the signed `room` claim."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from daimon.core.audience_facts import RoomFacts
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_identity import (
    RelayBinding,
    generate_signing_key,
    routine_destination_key,
    routine_revision,
)
from daimon.core.stores.audience_facts import StoredRoom, publish_facts
from daimon.core.stores.routines import get_routine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_identity_relay import (
    OWNER,
    SERVER,
    _call,
    _chat_session,
    _dm_fire,
    _identity,
    _owner,
    _Person,
    _person,
    _post,
    _relay,
    _routine,
    _token,
    _tool_call,
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


async def test_a_routine_run_signs_its_revision_and_name_and_no_revision_while_paused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`rev` binds a server's approval of the routine: kept across a pause, a resume, a new
    schedule and name; moved by a new prompt or agent name; never on a paused run, a
    person's turn, or a server without `routine_claims`."""
    factory = db_session_factory
    person = await _person(factory)
    await _facts(factory, person, {"chan-1": TEAM})
    routine_id = await _routine(factory, person, **_CHANNEL, name="Weekly links")
    async with factory() as db:
        row = await get_routine(db, routine_id, tenant_id=person.tenant_id)
    assert row is not None
    token = await _routine_token(
        factory, person, routine_id, owner="user-1", destination=routine_destination_key(row)
    )
    first = await _claims_of(factory, token, ROUTINE_SERVER)
    assert first["rev"] == routine_revision(row, workspace="discord:guild-1")
    assert first["routine_name"] == "Weekly links"
    await _sql(factory, "UPDATE routines SET enabled = false WHERE id = :r", r=routine_id)
    paused = await _claims_of(factory, token, ROUTINE_SERVER)
    assert paused["routine"] == str(routine_id) and "rev" not in paused, "paused: no revision"
    await _sql(
        factory,
        "UPDATE routines SET enabled = true, cron_expr = '0 8 * * 1', name = 'Links' WHERE id = :r",
        r=routine_id,
    )
    resumed = await _claims_of(factory, token, ROUTINE_SERVER)
    assert (resumed["rev"], resumed["routine_name"]) == (first["rev"], "Links")
    off = await _claims_of(factory, token, SERVER)
    assert not {"routine", "rev", "routine_name"} & set(off), "without the setting, nothing new"
    for edit in ("trigger_message = 'go now'", "agent_name = 'Agent B'"):
        await _sql(factory, f"UPDATE routines SET {edit} WHERE id = :r", r=routine_id)
        moved = await _claims_of(factory, token, ROUTINE_SERVER)
        assert moved["rev"] != first["rev"], edit
    await _sql(
        factory, "UPDATE routines SET created_by_user_id = 'user-2' WHERE id = :r", r=routine_id
    )
    handed = await _claims_of(factory, token, ROUTINE_SERVER)
    assert not {"routine", "rev"} & set(handed), "the old owner's run signs no routine"
    turn, _ = await _chat_session(factory, person, "chan-1", message="m-1")
    assert not {"rev", "routine_name"} & set(await _claims_of(factory, turn, ROUTINE_SERVER))


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


# A routine posting to its owner's DM

DM_SERVER = SERVER.model_copy(update={"routine_claims": True, "dm_routine_claims": True})
#: Claims that differ on every token.
_PER_TOKEN = {"iat", "exp", "jti"}


async def _signed(
    factory: async_sessionmaker[AsyncSession],
    token: str,
    server: SignedIdentityServer,
    body: str | None = None,
) -> tuple[dict[str, object], bytes]:
    """The claims `server` is sent for `token`, less those every token has its own of, and
    the body it is sent."""
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(factory, upstream, key, server) as client:
        await (_call(client, token) if body is None else _post(client, token, body))
    (sent,) = upstream.requests
    claims = {k: v for k, v in _identity(sent, key).items() if k not in _PER_TOKEN}
    return claims, sent.content


async def _sql(factory: async_sessionmaker[AsyncSession], sql: str, **params: object) -> None:
    async with factory() as db, db.begin():
        await db.execute(text(sql), params)


async def test_a_live_dm_routine_fire_names_its_routine_owner_and_dm(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _owner(db_session_factory)
    routine_id, token, _ = await _dm_fire(db_session_factory, person)
    on, _ = await _signed(db_session_factory, token, DM_SERVER)
    off, _ = await _signed(db_session_factory, token, SERVER)
    async with db_session_factory() as db:
        row = await get_routine(db, routine_id, tenant_id=person.tenant_id)
    assert row is not None
    rev = routine_revision(row, workspace="discord:guild-1")
    assert on == {**off, "routine": str(routine_id), "channel": "555", "room": "dm", "rev": rev}, (
        "today's claims, with the routine, the owner's DM, room dm and the routine's revision"
    )
    assert (on["sub"], on["ws"]) == (f"discord:{OWNER}", "discord:guild-1"), "the owner"
    assert not {"msg", "wake"} & set(on), "no message and no wake"
    every, _ = await _signed(
        db_session_factory, token, DM_SERVER.model_copy(update={"wake_claims": True})
    )
    assert every == on, "wake claims on as well add no wake to a routine"


_CHECKS = {
    "finished": "DELETE FROM lead_intake_executions",
    "expired": "UPDATE lead_intake_executions SET expires_at = now() - interval '1 second'",
    "another-routines-fire": "UPDATE lead_intake_executions SET routine_id = :other",
    "paused": "UPDATE routines SET enabled = false WHERE id = :routine",
    "edited": "UPDATE routines SET updated_at = updated_at + interval '1 microsecond'"
    " WHERE id = :routine",
    "handed-to-another-owner": "UPDATE routines SET created_by_user_id = '5678',"
    " destination_id = '5678' WHERE id = :routine",
    "another-agent": "UPDATE routines SET agent_id = 'agent_b' WHERE id = :routine",
    "now-to-a-channel": "UPDATE routines SET destination_kind = 'channel',"
    " destination_id = 'chan-1' WHERE id = :routine",
    "a-persons-dm-turn": "UPDATE turn_origins SET human_invocation = true, attended = true,"
    " platform_message_id = 'm-1'",
    "a-dm-watch-or-timer": "UPDATE turn_origins SET own_dm_reach = false",
    "a-helper": "UPDATE turn_origins SET helper_of = :parent, helper_key = 'k'",
    "grant-moved": "UPDATE turn_origins SET chat_session_id = 'g-other'",
    "foreign-sender": "UPDATE turn_origins SET sender_foreign = true",
    "another-signed-in-person": "UPDATE platform_principals SET external_id = '5678'"
    " WHERE account_id = :account",
}


@pytest.mark.parametrize("case", [*_CHECKS, "grant-less", "routine-claims-only", "both-off"])
async def test_a_dm_routine_call_any_check_fails_signs_what_it_signs_today(
    db_session_factory: async_sessionmaker[AsyncSession], case: str
) -> None:
    person = await _owner(db_session_factory)
    routine_id, token, _ = await _dm_fire(db_session_factory, person, combined=case != "grant-less")
    if case in _CHECKS:
        other = await _routine(db_session_factory, person, destination_kind="dm", owner=OWNER)
        await _sql(
            db_session_factory, _CHECKS[case], routine=routine_id, other=other,
            parent=uuid.uuid4(), account=person.account_id,
        )  # fmt: skip
    server = {"routine-claims-only": ROUTINE_SERVER, "both-off": SERVER}.get(case, DM_SERVER)
    on, _ = await _signed(db_session_factory, token, server)
    off, _ = await _signed(db_session_factory, token, SERVER)
    assert "routine" not in on and on.get("room") != "dm", f"{case}: no DM routine claims"
    assert on == off, f"{case}: the call signs what it signs without the setting"


@pytest.mark.parametrize(
    "server",
    [
        SERVER.model_copy(update={"requester_meta": True}),
        ROUTINE_SERVER.model_copy(update={"requester_meta": True}),
        DM_SERVER.model_copy(update={"requester_meta": True}),
    ],
    ids=["signed", "routine-claims", "dm-routine-claims"],
)
async def test_a_dm_routines_credential_stamps_as_today_and_signs_more_only_with_the_setting(
    db_session_factory: async_sessionmaker[AsyncSession], server: SignedIdentityServer
) -> None:
    """The fields a fire adds to its credential change nothing a server gets unless it
    signs DM routines; the requester fields it is stamped with never change."""
    person = await _owner(db_session_factory)
    routine_id, combined, today = await _dm_fire(db_session_factory, person)
    async with db_session_factory() as db:
        row = await get_routine(db, routine_id, tenant_id=person.tenant_id)
    assert row is not None
    on_rev = routine_revision(row, workspace="discord:guild-1")
    body = _tool_call(requester_platform_user_id="666", requester_kind="person")
    new, new_body = await _signed(db_session_factory, combined, server, body)
    old, old_body = await _signed(db_session_factory, today, server, body)
    assert new_body == old_body, "byte for byte today's body and requester stamp"
    assert json.loads(new_body)["params"]["_meta"]["requester_kind"] == "routine"
    added = {"routine": str(routine_id), "channel": "555", "room": "dm", "rev": on_rev}
    assert new == ({**old, **added} if server.dm_routine_claims else old), (
        "only a server signing DM routines gets more"
    )
