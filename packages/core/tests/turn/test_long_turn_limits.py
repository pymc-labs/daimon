"""A turn allowed past the standard 45 minutes stays live in every check that assumed 45."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core import direct_messages, session_fence_retry
from daimon.core.session_preparation_stages import turn_blocks_change, turn_is_active
from daimon.core.stores.direct_messages import (
    DirectMessageBusy,
    DirectMessageRow,
    claim_message,
    extend_claim,
    get_conversation,
    holds_claim,
    start_conversation,
)
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
from daimon.testing.turn_fakes import deps_for
from sqlalchemy import text
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


async def _claim(
    session: AsyncSession, row: DirectMessageRow, message_id: str, *, at: datetime, minutes: int
) -> DirectMessageRow | None:
    return await claim_message(
        session,
        platform=row.platform,
        route_key=row.route_key,
        external_user_id=row.external_user_id,
        message_id=message_id,
        expected_scope_id=row.scope_id,
        now=at,
        active_until=at + timedelta(minutes=minutes),
    )


async def test_a_dm_claim_left_by_a_dead_worker_does_not_block_for_hours(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The next message, holding the owner's fence, takes over a 3-hour claim nobody runs."""
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        original = await make_dm_conversation(session, tenant=tenant, account_id=account.id)
        left = await _claim(session, original, "m1", at=_NOW, minutes=181)
    assert left is not None, "the first message claims the conversation"
    later = _NOW + timedelta(minutes=10)
    async with db_session_factory.begin() as session:
        taken = await _claim(session, original, "m2", at=later, minutes=46)
        assert taken is not None, "the next message is admitted, not refused as busy"
        assert taken.active_until == later + timedelta(minutes=46), "it holds its own claim"
        assert not await holds_claim(session, conversation=left), "the dead claim lost the scope"


async def test_a_new_dm_conversation_waits_only_for_a_live_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        original = await make_dm_conversation(session, tenant=tenant, account_id=account.id)
        claimed = await _claim(session, original, "m1", at=_NOW, minutes=181)
    assert claimed is not None, "the turn claims the conversation"
    fresh = original.model_copy(update={"scope_id": "dm:fresh", "history": []})
    later = _NOW + timedelta(minutes=10)
    deps = deps_for(None, db_session_factory)
    async with direct_messages.owner_dm_turn(
        deps, platform=original.platform, owner=original.external_user_id, wait=False
    ):
        with pytest.raises(DirectMessageBusy):
            async with db_session_factory.begin() as session:
                await start_conversation(session, conversation=fresh, now=later)
    async with db_session_factory.begin() as session:
        await start_conversation(session, conversation=fresh, now=later)
    async with db_session_factory() as session:
        stored = await get_conversation(
            session,
            platform=fresh.platform,
            route_key=fresh.route_key,
            external_user_id=fresh.external_user_id,
        )
    assert stored is not None and stored.scope_id == "dm:fresh", "a gone turn's claim is replaced"


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


async def test_a_dead_workers_long_marker_stops_blocking(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 180-minute marker whose worker is gone must not hold the session for hours."""
    dead_key, live_key = 7_100_001, 7_100_002
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        rows = [
            await make_thread_session(session, tenant=tenant, account=account) for _ in range(3)
        ]
        for row, key in zip(rows, (dead_key, live_key, None), strict=True):
            await mark_turn_active(
                session,
                id=row.id,
                active_turn_message_id="m1",
                now=datetime.now(UTC),
                owner_key=key,
                deadline=datetime.now(UTC) + timedelta(minutes=180),
            )
    async with db_session_factory() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(:key)"), {"key": live_key})
        try:
            async with db_session_factory() as session:
                dead, live, unowned = [
                    await get_thread_session_by_id(session, id=row.id) for row in rows
                ]
            assert dead is not None and live is not None and unowned is not None
            now = datetime.now(UTC)
            blocks = [
                await turn_blocks_change(row, now=now, sessionmaker=db_session_factory)
                for row in (dead, live, unowned)
            ]
        finally:
            await holder.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": live_key})
    assert blocks == [False, True, True], "only the marker of a worker that is gone is released"


async def test_an_owner_dm_waits_no_longer_than_the_standard_window(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float | None] = []
    real = session_fence_retry.try_fence

    async def record(*args: object, timeout_s: float | None = None, **kwargs: object) -> None:
        waits.append(timeout_s)
        await real(*args, timeout_s=timeout_s, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(direct_messages, "try_fence", record)
    deps = deps_for(None, db_session_factory)
    async with direct_messages.owner_dm_turn(deps, platform="discord", owner="777", wait=True):
        pass
    assert waits == [TURN_CEILING_S], "a DM send never queues for hours behind a long turn"
