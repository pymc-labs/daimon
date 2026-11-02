"""Real-Postgres tests for the browser session store."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from daimon.core.stores import web_people as people
from daimon.core.stores import web_sessions as sessions
from daimon.testing.factories import make_web_person
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_TTL = timedelta(days=30)


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def _start(session: AsyncSession, token: str, *people):
    return await sessions.create_session(
        session,
        token_sha256=_digest(token),
        provider="oidc",
        issuer="https://id.example.com",
        subject="sub",
        people=list(people),
        ttl=_TTL,
        now=_NOW,
    )


async def test_session_is_found_by_hash_until_it_expires(db_session: AsyncSession) -> None:
    person = await make_web_person(db_session)
    created = await _start(db_session, "t1", person)

    live = await sessions.get_live_session(db_session, token_sha256=_digest("t1"), now=_NOW)
    late = await sessions.get_live_session(
        db_session, token_sha256=_digest("t1"), now=_NOW + _TTL + timedelta(seconds=1)
    )
    unknown = await sessions.get_live_session(db_session, token_sha256=_digest("t2"), now=_NOW)

    assert live == created, "the live session is found by the token's hash"
    assert created.current_person_id == person.id, "the first person is current"
    assert late is None, "an expired session is not live"
    assert unknown is None, "an unknown token finds nothing"


async def test_session_starts_on_the_first_active_person(db_session: AsyncSession) -> None:
    suspended = await make_web_person(db_session)
    active = await make_web_person(db_session)
    await people.update_person(db_session, person_id=suspended.id, status="suspended")
    suspended = suspended.model_copy(update={"status": "suspended"})

    started = await _start(db_session, "t1", suspended, active)
    all_suspended = await _start(db_session, "t2", suspended)

    assert started.current_person_id == active.id, "a suspended person is not the starting point"
    assert started.tenant_id == active.tenant_id, "the session follows the person's workspace"
    assert all_suspended.current_person_id == suspended.id, (
        "with nobody active the session still opens, and says why the person is refused"
    )


async def test_touch_records_the_visit_without_extending_the_expiry(
    db_session: AsyncSession,
) -> None:
    person = await make_web_person(db_session)
    created = await _start(db_session, "t1", person)
    later = _NOW + timedelta(days=20)

    await sessions.touch(db_session, session_id=created.id, now=later)

    visited = await sessions.get_live_session(db_session, token_sha256=_digest("t1"), now=later)
    after = await sessions.get_live_session(
        db_session, token_sha256=_digest("t1"), now=_NOW + _TTL + timedelta(seconds=1)
    )
    assert visited is not None and visited.last_seen_at == later, "the visit is recorded"
    assert visited.expires_at == _NOW + _TTL, "a visit must not push the expiry out"
    assert after is None, "the session ends one lifetime after sign-in, however often it is used"


async def test_mark_checked_moves_the_recheck_time(db_session: AsyncSession) -> None:
    person = await make_web_person(db_session)
    created = await _start(db_session, "t1", person)
    later = _NOW + timedelta(minutes=16)

    await sessions.mark_checked(db_session, session_id=created.id, now=later)

    live = await sessions.get_live_session(db_session, token_sha256=_digest("t1"), now=later)
    assert live is not None and live.roles_checked_at == later, "the check time is recorded"


async def test_switch_person_only_reaches_workspaces_the_sign_in_reached(
    db_session: AsyncSession,
) -> None:
    first = await make_web_person(db_session)
    second = await make_web_person(db_session)
    stranger = await make_web_person(db_session)
    created = await _start(db_session, "t1", first, second)

    switched = await sessions.switch_person(
        db_session, session_id=created.id, tenant_id=second.tenant_id
    )
    refused = await sessions.switch_person(
        db_session, session_id=created.id, tenant_id=stranger.tenant_id
    )

    assert switched is not None and switched.current_person_id == second.id, "switch succeeds"
    assert switched.tenant_id == second.tenant_id, "the session follows the workspace"
    assert refused is None, "a workspace outside the sign-in cannot be switched to"


async def test_delete_session_and_expired_sweep(db_session: AsyncSession) -> None:
    person = await make_web_person(db_session)
    await _start(db_session, "old", person)
    await _start(db_session, "gone", person)

    swept = await sessions.delete_expired(db_session, now=_NOW + _TTL + timedelta(days=1))
    removed = await sessions.delete_session(db_session, token_sha256=_digest("gone"))

    assert swept == 2, "both sessions had expired"
    assert removed == 0, "an already-swept session deletes nothing"
