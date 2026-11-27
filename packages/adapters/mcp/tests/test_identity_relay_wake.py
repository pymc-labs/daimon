"""The relay signs `wake` on a call with no fresh message only where its own checks pass:
a person's turn still running, or a queued continuation running in its own thread."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import cast, get_args

import pytest
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_identity import RelayBinding, generate_signing_key
from daimon.core.stores.domain import ContinuationReason, Role
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import bind_origin_session, create_origin
from daimon.testing.factories import make_task_continuation, make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_identity_relay import (
    SERVER,
    _call,
    _chat_session,
    _colleague,
    _identity,
    _Person,
    _person,
    _post,
    _relay,
    _token,
    _Upstream,
)
from .test_identity_relay_room import NARROW, ROUTINE_SERVER, TEAM, _facts, _routine_claims

WAKE_SERVER = SERVER.model_copy(update={"wake_claims": True})
BOTH_SERVER = SERVER.model_copy(update={"wake_claims": True, "routine_claims": True})
REASONS: tuple[str, ...] = get_args(ContinuationReason)
#: Claims that differ on every token.
_PER_TOKEN = {"iat", "exp", "jti"}


async def _claims(
    factory: async_sessionmaker[AsyncSession],
    token: str,
    server: SignedIdentityServer,
    body: str | None = None,
) -> dict[str, object]:
    upstream, key = _Upstream(), generate_signing_key()
    async with _relay(factory, upstream, key, server) as client:
        await (_call(client, token) if body is None else _post(client, token, body))
    return {k: v for k, v in _identity(upstream.requests[0], key).items() if k not in _PER_TOKEN}


async def _today(
    factory: async_sessionmaker[AsyncSession], token: str, server: SignedIdentityServer
) -> tuple[dict[str, object], dict[str, object]]:
    """What `server` signs for `token`, and what the same server without wakes signs."""
    off = server.model_copy(update={"wake_claims": False})
    return await _claims(factory, token, server), await _claims(factory, token, off)


async def _sql(factory: async_sessionmaker[AsyncSession], sql: str, **params: object) -> None:
    async with factory() as db, db.begin():
        await db.execute(text(sql), params)


async def _continuation(
    factory: async_sessionmaker[AsyncSession],
    person: _Person,
    *,
    reason: str = "timer",
    thread: str = "thr-1",
    parent: str = "chan-1",
    human: bool = False,
    grant: str = "g-wake",
) -> str:
    """A queued continuation's turn running in `thread`: a claimed, started row under a
    live lease, and the origin that took the claim's owner as its id, holding the session's
    grant, with no message. Returns the session's relay credential."""
    now = dt.datetime.now(dt.UTC)
    owner, mapping_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as db, db.begin():
        await create_thread_session(
            db, id=mapping_id, tenant_id=person.tenant_id, platform="discord",
            thread_id=thread, account_id=person.account_id, ma_session_id=f"sess_{grant}",
            ma_agent_id="agent_a",
        )  # fmt: skip
        origin = await create_origin(
            db, tenant_id=person.tenant_id, account_id=person.account_id, platform="discord",
            parent_channel_id=parent, thread_id=thread, responder_ma_agent_id="agent_a",
            responder_name="Helper", configuration_target_ma_agent_id=None,
            configuration_target_name=None, role=Role.USER,
            expires_at=now + dt.timedelta(hours=1), now=now, sender_foreign=False,
            shared_channel=False, attended=human, platform_channel_id=thread, origin_id=owner,
        )  # fmt: skip
        await bind_origin_session(
            db, origin_id=origin.id, chat_session_id=grant, human_invocation=human
        )
        row = await make_task_continuation(
            db, tenant_id=person.tenant_id, platform="discord", parent_channel_id=parent,
            thread_id=thread, requester_account_id=person.account_id,
            requester_external_user_id="user-1", target_ma_agent_id="agent_a",
            target_name="Helper", requested_work="check again",
            reason=cast(ContinuationReason, reason), idempotency_key=uuid.uuid4(),
        )  # fmt: skip
        await db.execute(  # claimed and started under this owner's live lease
            text(
                "UPDATE task_continuations SET status = 'claimed', claimed_at = :now,"
                " started_at = :now, lease_owner = :owner, lease_expires_at = :until,"
                " attempts = 1 WHERE id = :id"
            ),
            {"now": now, "owner": owner.hex, "until": now + dt.timedelta(minutes=5), "id": row.id},
        )
    return await _token(factory, person, RelayBinding("Agent A", mapping_id, grant=grant))


# A person's live turn


async def test_a_live_turn_holding_its_grant_signs_wake_turn_and_everything_else_unchanged(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token, _ = await _chat_session(db_session_factory, person, "chan-1", grant="g-1")
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert on == {**off, "wake": "turn"}, "the same claims, with wake"
    assert (on["sub"], on["channel"], on["msg"], on["room"]) == (
        "discord:user-1",
        "chan-1",
        "msg-chan-1",
        "team",
    )


@pytest.mark.parametrize("case", ["grant-less", "expired", "helper", "foreign", "not-a-person"])
async def test_a_turn_the_relay_cannot_vouch_for_signs_no_wake_and_what_it_signs_today(
    db_session_factory: async_sessionmaker[AsyncSession], case: str
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token, _ = await _chat_session(
        db_session_factory,
        person,
        "chan-1",
        grant=None if case == "grant-less" else "g-1",
        foreign=case == "foreign",
        human=case != "not-a-person",
    )
    if case == "expired":
        await _sql(db_session_factory, "UPDATE turn_origins SET expires_at = now() - interval '1s'")
    if case == "helper":
        # Found through the grant in its own session's thread, but a helper's.
        await _sql(
            db_session_factory,
            "UPDATE turn_origins SET helper_of = :parent, helper_key = 'chan-1'",
            parent=uuid.uuid4(),
        )
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert "wake" not in on, f"{case}: no wake"
    assert on == off, f"{case}: the call signs what it signs today"


async def test_a_wake_written_in_tool_arguments_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    granted, _ = await _chat_session(db_session_factory, person, "chan-1", grant="g-1")
    grantless, _ = await _chat_session(db_session_factory, person, "chan-2")
    forged = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "search",
            "arguments": {"wake": "timer", "room": "team", "routine": "r-1", "channel": "x"},
            "_meta": {"wake": "timer", "room": "team"},
        },
    })  # fmt: skip
    for token, wake in ((granted, "turn"), (grantless, None)):
        plain = await _claims(db_session_factory, token, WAKE_SERVER)
        assert plain.get("wake") == wake
        assert await _claims(db_session_factory, token, WAKE_SERVER, forged) == plain, (
            "the claim comes from the relay's checks alone"
        )


# A continuation running in its thread


@pytest.mark.parametrize("reason", REASONS)
async def test_each_continuation_reason_signs_its_thread_as_a_routine_with_no_person(
    db_session_factory: async_sessionmaker[AsyncSession], reason: str
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM, "thr-1": NARROW})
    token = await _continuation(db_session_factory, person, reason=reason)
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert (on["wake"], on["channel"], on["room"], on["ws"]) == (
        reason,
        "thr-1",
        "narrow",
        "discord:guild-1",
    )
    assert not {"sub", "msg", "routine"} & set(on), "no person, no message, no routine"
    assert off["sub"] == "discord:user-1", "today it names the requester and reads nothing"


async def test_a_continuation_a_person_started_with_no_message_still_signs_its_reason(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A hand-off can be a person's turn (`continuation_attended`), with no message id."""
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token = await _continuation(db_session_factory, person, reason="task_handoff", human=True)
    claims = await _claims(db_session_factory, token, WAKE_SERVER)
    assert claims["wake"] == "task_handoff" and "sub" not in claims


async def test_a_continuation_in_a_public_thread_with_no_fact_reads_narrow_like_a_routine(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token = await _continuation(db_session_factory, person)
    await _sql(db_session_factory, "UPDATE turn_origins SET public_thread = true")
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert off["room"] == "team", "the turn alone takes its public thread's parent"
    assert (on["wake"], on["room"]) == ("timer", "narrow"), (
        "a routine's thread rule, not the turn's"
    )


async def test_a_continuation_in_the_channel_itself_signs_no_wake(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token = await _continuation(db_session_factory, person, thread="chan-1")
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert "wake" not in on and on == off, "only a thread, never its channel"


_ROW_CHANGES = {
    "released": "status = 'pending', lease_owner = NULL, lease_expires_at = NULL,"
    " started_at = NULL",
    "expired": "lease_expires_at = now() - interval '1s'",
    "taken-over": "lease_owner = md5('another claim')",
    "not-started": "started_at = NULL",
    "settled": "status = 'delivered'",
    "another-requester": "requester_account_id = :colleague",
    "another-requester-id": "requester_external_user_id = 'user-2'",
    "another-agent": "target_ma_agent_id = 'agent_b'",
    "another-tenant": "tenant_id = :tenant",
    "another-thread": "thread_id = 'thr-0'",
    "another-parent": "parent_channel_id = 'chan-0'",
}


@pytest.mark.parametrize("case", list(_ROW_CHANGES))
async def test_a_row_that_no_longer_names_this_turn_signs_no_wake(
    db_session_factory: async_sessionmaker[AsyncSession], case: str
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token = await _continuation(db_session_factory, person)
    params: dict[str, object] = {}
    if case == "another-requester":
        params["colleague"] = (await _colleague(db_session_factory, person)).account_id
    if case == "another-tenant":
        async with db_session_factory() as db, db.begin():
            params["tenant"] = (await make_tenant(db, platform="discord", workspace_id="g-2")).id
    await _sql(db_session_factory, f"UPDATE task_continuations SET {_ROW_CHANGES[case]}", **params)
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert "wake" not in on, f"{case}: no wake"
    assert on == off, f"{case}: the call signs what it signs today"


@pytest.mark.parametrize(
    ("case", "thread", "moved"),
    [
        # The turn and its session run in the new thread; the row names the watch's thread.
        ("new-thread-report", "thr-1", "thr-new"),
        ("dm-scope", "dm:user-1", None),
        ("channel-scope", "ch:chan-1:1", None),
    ],
)
async def test_a_turn_outside_its_rows_own_thread_signs_no_wake(
    db_session_factory: async_sessionmaker[AsyncSession],
    case: str,
    thread: str,
    moved: str | None,
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM, "thr-new": TEAM})
    token = await _continuation(db_session_factory, person, reason="watch_report", thread=thread)
    if moved is not None:
        await _sql(
            db_session_factory,
            "UPDATE turn_origins SET thread_id = :t, platform_channel_id = :t",
            t=moved,
        )
        await _sql(db_session_factory, "UPDATE thread_sessions SET thread_id = :t", t=moved)
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert "wake" not in on, f"{case}: no wake"
    assert on == off, f"{case}: the call signs what it signs today"


async def test_a_continuation_whose_signed_channel_is_not_its_thread_signs_no_wake(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token = await _continuation(db_session_factory, person)
    await _sql(db_session_factory, "UPDATE turn_origins SET platform_channel_id = 'chan-1'")
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert "wake" not in on and on == off


async def test_a_helpers_turn_in_a_wakes_thread_signs_no_wake(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    token = await _continuation(db_session_factory, person)
    await _sql(
        db_session_factory,
        "UPDATE turn_origins SET helper_of = :parent, helper_key = 'thr-1'",
        parent=uuid.uuid4(),
    )
    on, off = await _today(db_session_factory, token, WAKE_SERVER)
    assert "wake" not in on and on == off


# The two settings


async def test_the_two_settings_are_independent_and_never_combine(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    person = await _person(db_session_factory)
    await _facts(db_session_factory, person, {"chan-1": TEAM})
    turn, _ = await _chat_session(db_session_factory, person, "chan-1", grant="g-1")
    wake = await _continuation(db_session_factory, person)
    routine_fields: dict[str, object] = {"destination_kind": "channel", "destination_id": "chan-1"}
    for server, wakes, routines in (
        (SERVER, False, False),
        (ROUTINE_SERVER, False, True),
        (WAKE_SERVER, True, False),
        (BOTH_SERVER, True, True),
    ):
        turn_claims = await _claims(db_session_factory, turn, server)
        wake_claims = await _claims(db_session_factory, wake, server)
        _, routine_claims = await _routine_claims(
            db_session_factory, person, routine_fields, server=server
        )
        assert turn_claims.get("wake") == ("turn" if wakes else None)
        assert wake_claims.get("wake") == ("timer" if wakes else None)
        assert ("routine" in routine_claims) is routines
        assert "wake" not in routine_claims, "a routine run never signs a wake"
        assert "routine" not in turn_claims and "routine" not in wake_claims
