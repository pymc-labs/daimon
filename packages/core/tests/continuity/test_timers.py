"""One-shot timers on the wake queue, against real Postgres.

Exit check for FEAT-084: a timer fires once, at its time, and a cancelled
timer never fires.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.core.continuity.continuation import ContinuationRequest, decide_continuation
from daimon.core.continuity.messages import render_timer_seed
from daimon.core.continuity.timers import (
    MAX_PENDING_TIMERS,
    TimerError,
    cancel_timer,
    list_timers,
    parse_fire_at,
    schedule_timer,
)
from daimon.core.continuity.wakes import (
    claim_wake,
    list_due_wake_threads,
    settle_wake,
    start_wake,
)
from daimon.core.stores.task_continuations import get_continuation
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


async def _schedule(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    account_id: uuid.UUID,
    fire_at: datetime,
    note: str = "check whether the nightly build went green",
) -> uuid.UUID:
    return await schedule_timer(
        sessionmaker,
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id="channel-1",
        thread_id="thread-1",
        requester_account_id=account_id,
        requester_external_user_id="discord-user-1",
        target_ma_agent_id="agent_stats",
        target_name="stats-bot",
        note=note,
        fire_at=fire_at,
    )


async def _tenant(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessionmaker.begin() as session:
        return (await make_tenant(session)).id


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("tomorrow at nine", "not an ISO 8601"),
        ("2026-09-28T15:00:00", "needs a UTC offset"),
        ("2026-09-28T12:00:30+00:00", "at least one minute"),
        ("2027-09-28T12:00:00+00:00", "within 90 days"),
    ],
)
def test_parse_fire_at_refuses_what_it_cannot_schedule(value: str, message: str) -> None:
    with pytest.raises(TimerError, match=message):
        parse_fire_at(value, now=_NOW)


def test_parse_fire_at_normalises_the_offset_to_utc() -> None:
    assert parse_fire_at("2026-09-28T16:00:00+02:00", now=_NOW) == _NOW + timedelta(hours=2)


async def test_a_timer_fires_once_at_its_time(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    fire_at = _NOW + timedelta(hours=2)
    timer_id = await _schedule(
        db_session_factory, tenant_id, account_id=uuid.uuid4(), fire_at=fire_at
    )

    early = fire_at - timedelta(seconds=1)
    assert await list_due_wake_threads(db_session_factory, platform="discord", now=early) == []
    assert await claim_wake(db_session_factory, idempotency_key=timer_id, now=early) is None

    claims = [
        await claim_wake(db_session_factory, idempotency_key=timer_id, now=fire_at)
        for _ in range(3)
    ]
    assert sum(claim is not None for claim in claims) == 1, (
        "a timer fires exactly once (the cross-connection race is in test_wakes)"
    )
    winner = next(claim for claim in claims if claim is not None)
    assert await start_wake(db_session_factory, winner, now=fire_at)
    assert await settle_wake(db_session_factory, winner, status="delivered", now=fire_at)
    later = fire_at + timedelta(days=1)
    assert await claim_wake(db_session_factory, idempotency_key=timer_id, now=later) is None
    assert await list_due_wake_threads(db_session_factory, platform="discord", now=later) == []


async def test_a_cancelled_timer_never_fires(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    owner, stranger = uuid.uuid4(), uuid.uuid4()
    fire_at = _NOW + timedelta(hours=2)
    timer_id = await _schedule(db_session_factory, tenant_id, account_id=owner, fire_at=fire_at)

    assert not await cancel_timer(
        db_session_factory,
        tenant_id=tenant_id,
        timer_id=timer_id,
        account_id=stranger,
        is_admin=False,
    ), "only the person who set it (or an admin) may cancel it"
    assert await cancel_timer(
        db_session_factory, tenant_id=tenant_id, timer_id=timer_id, account_id=owner, is_admin=False
    )
    assert not await cancel_timer(
        db_session_factory, tenant_id=tenant_id, timer_id=timer_id, account_id=owner, is_admin=False
    ), "cancelling twice reports nothing to cancel"

    assert await claim_wake(db_session_factory, idempotency_key=timer_id, now=fire_at) is None
    assert await list_due_wake_threads(db_session_factory, platform="discord", now=fire_at) == []
    assert await list_timers(db_session_factory, tenant_id=tenant_id, account_id=owner) == []


async def test_a_fired_timer_can_no_longer_be_cancelled(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    owner = uuid.uuid4()
    fire_at = _NOW + timedelta(hours=2)
    timer_id = await _schedule(db_session_factory, tenant_id, account_id=owner, fire_at=fire_at)
    assert await claim_wake(db_session_factory, idempotency_key=timer_id, now=fire_at)

    assert not await cancel_timer(
        db_session_factory, tenant_id=tenant_id, timer_id=timer_id, account_id=owner, is_admin=True
    )


async def test_list_timers_is_the_callers_pending_timers_soonest_first(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    owner = uuid.uuid4()
    later = await _schedule(
        db_session_factory, tenant_id, account_id=owner, fire_at=_NOW + timedelta(hours=5)
    )
    sooner = await _schedule(
        db_session_factory, tenant_id, account_id=owner, fire_at=_NOW + timedelta(hours=1)
    )
    await _schedule(
        db_session_factory, tenant_id, account_id=uuid.uuid4(), fire_at=_NOW + timedelta(hours=1)
    )

    rows = await list_timers(db_session_factory, tenant_id=tenant_id, account_id=owner)
    assert [row.idempotency_key for row in rows] == [sooner, later]


async def test_pending_timers_per_person_are_capped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _tenant(db_session_factory)
    owner = uuid.uuid4()
    for _ in range(MAX_PENDING_TIMERS):
        await _schedule(
            db_session_factory, tenant_id, account_id=owner, fire_at=_NOW + timedelta(hours=1)
        )

    with pytest.raises(TimerError, match="already has"):
        await _schedule(
            db_session_factory, tenant_id, account_id=owner, fire_at=_NOW + timedelta(hours=1)
        )


async def test_a_fired_timer_dispatches_its_note_even_after_newer_messages(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unlike a handoff, a timer is not superseded by the person talking since it was set."""
    tenant_id = await _tenant(db_session_factory)
    fire_at = _NOW + timedelta(hours=2)
    timer_id = await _schedule(
        db_session_factory, tenant_id, account_id=uuid.uuid4(), fire_at=fire_at
    )
    async with db_session_factory() as session:
        row = await get_continuation(session, idempotency_key=timer_id)
    assert row is not None
    agent = ma_agent(id="agent_stats", name="stats-bot", tenant_id=tenant_id)
    anthropic = build_stub_anthropic(
        lambda _request: httpx.Response(200, json=agent.model_dump(mode="json"))
    )
    request = ContinuationRequest(
        tenant_id=tenant_id,
        platform="discord",
        parent_channel_id=row.parent_channel_id,
        thread_id=row.thread_id,
        requester_account_id=row.requester_account_id,
        requester_external_user_id=row.requester_external_user_id,
        target_ma_agent_id=row.target_ma_agent_id,
        target_name=row.target_name,
        requested_work=row.requested_work,
        reason="timer",
        idempotency_key=timer_id,
    )

    decision = await decide_continuation(
        db_session_factory,
        anthropic,
        request=request,
        now=fire_at,
        latest_user_message_at=fire_at - timedelta(minutes=5),
        active_turn=False,
    )

    assert decision.action == "dispatch"
    assert decision.seed_user_message == render_timer_seed(
        "check whether the nightly build went green", set_at=row.created_at
    )
