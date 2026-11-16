"""A turn allowed past the standard 45 minutes stays live in every check that assumed 45."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core import session_fence_retry
from daimon.core.session_preparation_stages import turn_is_active
from daimon.core.stores.direct_messages import claim_message, extend_claim
from daimon.core.stores.thread_sessions import (
    clear_active_turn,
    get_thread_session_by_id,
    mark_turn_active,
)
from daimon.core.turn.ceiling import (
    LONGEST_TURN_CEILING_S,
    TURN_CEILING_S,
    current_turn_deadline,
    marker_window_s,
    turn_deadline,
)
from daimon.testing.factories import (
    make_account,
    make_dm_conversation,
    make_tenant,
    make_thread_session,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


def test_the_marker_window_follows_the_turns_own_deadline() -> None:
    ninety = _NOW + timedelta(minutes=90)
    assert marker_window_s(started_at=_NOW, deadline=None) == TURN_CEILING_S, "legacy rows"
    assert marker_window_s(started_at=_NOW, deadline=ninety) == 90 * 60.0
    short = _NOW + timedelta(minutes=15)
    assert marker_window_s(started_at=_NOW, deadline=short) == TURN_CEILING_S, "never below 45"
    far = _NOW + timedelta(days=2)
    assert marker_window_s(started_at=_NOW, deadline=far) == LONGEST_TURN_CEILING_S


def test_retained_authority_still_caps_a_ninety_minute_limit() -> None:
    capped = turn_deadline(
        now=_NOW, ceiling_s=90 * 60.0, authority_deadline=_NOW + timedelta(minutes=20)
    )
    assert capped == _NOW + timedelta(minutes=20)


async def test_a_ninety_minute_marker_stays_active_past_45_minutes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        legacy = await make_thread_session(session)
        long = await make_thread_session(session)
        for mapping, deadline in ((legacy, None), (long, _NOW + timedelta(minutes=90))):
            await mark_turn_active(
                session,
                id=mapping.id,
                active_turn_message_id="m1",
                now=_NOW,
                deadline=deadline,
            )
    async with db_session_factory() as session:
        legacy = await get_thread_session_by_id(session, id=legacy.id)
        long = await get_thread_session_by_id(session, id=long.id)
    assert legacy is not None and long is not None
    assert legacy.active_turn_deadline is None
    for minutes, legacy_active, long_active in (
        (40, True, True),
        (50, False, True),
        (80, False, True),
        (95, False, False),
    ):
        now = _NOW + timedelta(minutes=minutes)
        assert turn_is_active(legacy, now=now) is legacy_active, f"legacy at {minutes} minutes"
        assert turn_is_active(long, now=now) is long_active, f"90-minute at {minutes} minutes"
    async with db_session_factory.begin() as session:
        await clear_active_turn(session, id=long.id)
    async with db_session_factory() as session:
        cleared = await get_thread_session_by_id(session, id=long.id)
    assert cleared is not None and cleared.active_turn_deadline is None


async def test_a_dm_claim_extends_to_the_admitted_deadline(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        original = await make_dm_conversation(session, tenant=tenant, account_id=account.id)
        claimed = await claim_message(
            session,
            platform=original.platform,
            route_key=original.route_key,
            external_user_id=original.external_user_id,
            message_id="m1",
            expected_scope_id=original.scope_id,
            now=_NOW,
            active_until=_NOW + timedelta(seconds=TURN_CEILING_S + 60),
        )
        assert claimed is not None
        same = await extend_claim(
            session,
            conversation=claimed,
            active_until=_NOW + timedelta(seconds=TURN_CEILING_S + 60),
        )
        assert same == claimed, "the default 45 minutes moves nothing"
        longer = await extend_claim(
            session, conversation=claimed, active_until=_NOW + timedelta(minutes=91)
        )
        assert longer.active_until == _NOW + timedelta(minutes=91)
        stale = await extend_claim(
            session, conversation=claimed, active_until=_NOW + timedelta(minutes=120)
        )
        assert stale == claimed, "a claim that was already moved is not the caller's any more"
    with pytest.raises(Exception, match="still running"):
        async with db_session_factory.begin() as session:
            await claim_message(
                session,
                platform=original.platform,
                route_key=original.route_key,
                external_user_id=original.external_user_id,
                message_id="m2",
                expected_scope_id=original.scope_id,
                now=_NOW + timedelta(minutes=60),
                active_until=_NOW + timedelta(minutes=200),
            )


class _Db:
    async def execute(self, *_args: object, **_kwargs: object) -> _Db:
        return self

    def scalar_one(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("turn_minutes", "expected_s"),
    [(None, TURN_CEILING_S), (90, 90 * 60.0), (15, TURN_CEILING_S)],
    ids=["outside-a-turn", "ninety-minute-turn", "short-turn"],
)
async def test_a_fence_waits_as_long_as_the_running_turn_may(
    monkeypatch: pytest.MonkeyPatch, turn_minutes: int | None, expected_s: float
) -> None:
    waits: list[float] = []
    real = asyncio.timeout_at
    loop = asyncio.get_running_loop()

    def record(when: float | None):
        assert when is not None
        waits.append(when - loop.time())
        return real(when)

    monkeypatch.setattr(asyncio, "timeout_at", record)
    token = current_turn_deadline.set(
        None if turn_minutes is None else datetime.now(UTC) + timedelta(minutes=turn_minutes)
    )
    try:
        await session_fence_retry.try_fence(_Db(), "k")  # type: ignore[arg-type]
    finally:
        current_turn_deadline.reset(token)
    assert len(waits) == 1 and abs(waits[0] - expected_s) < 5
