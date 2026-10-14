"""Actual DB claim/finalize + real Discord adapter posts; no native factory claim."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, NoReturn, cast

import discord
import pytest
from daimon.adapters.discord.routine_delivery import make_discord_routine_poster
from daimon.core._models import Routine
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import DirectMessagePolicy
from daimon.core.routine_delivery import DeliveryOutcome, poll_deliveries_once
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine, get_routine, record_result
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordSurfaceHarness, OfflineDiscordGateway
from daimon.testing.factories import make_tenant
from daimon.testing.routine_clock import (
    ClockedRoutineJobs,
    LongWaitUnavailable,
    RoutineBinding,
    RoutineJobClock,
)
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_ANCHOR = datetime(2024, 1, 1, tzinfo=UTC)
# Main 656f0ecd13698c1ec3ea77157db55b08f877d15d (#615, synced by #812):
# the complete stored reply is delivered with word-boundary splitting.
_EXPECTED_DELIVERY_CHUNKS = [
    "Result from daimon's routine (* * * * *, ",
    "UTC):\n\nROUTINE-PING" + "x" * 1977,
    "x" * 523,
]


class NoCaps:
    async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool:
        return False


async def unused_fire(row: RoutineRow) -> None:
    raise AssertionError("unexpected scheduled fire")


async def unused_post(row: RoutineRow) -> DeliveryOutcome:
    raise AssertionError("unexpected adapter post")


def _guard_engine(database: str) -> AsyncEngine:
    engine = create_async_engine(f"postgresql+asyncpg://nobody@localhost/{database}")

    def no_connect(*_: object) -> NoReturn:
        raise AssertionError("guard test attempted a physical connection")

    event.listen(engine.sync_engine, "do_connect", no_connect)
    return engine


def _runner(sm: async_sessionmaker[AsyncSession]) -> ClockedRoutineJobs:
    clock = RoutineJobClock(anchor=_ANCHOR)
    gateway = OfflineDiscordGateway(evidence_id="guard", channel_id=102, clock=clock)
    return ClockedRoutineJobs(
        sessionmaker=sm,
        clock=clock,
        caps=NoCaps(),
        fire=unused_fire,
        post=unused_post,
        gateway=gateway,
        bindings=(),
    )


@pytest.fixture
def owned_sm(
    db_preflight, db_engine: AsyncEngine, db_session: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    return _require_hook_sm(db_engine)


def _require_hook_sm(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        require_owned_sessionmaker(sm)
    except ValueError:
        if engine.url.database == "daimon_test" and not (
            os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
        ):
            pytest.skip("routine clock hooks refuse plain local daimon_test; use an owned lane DB")
        raise
    return sm


@pytest.mark.parametrize("github,ci", [("false", "false"), ("true", "false"), ("true", "true")])
def test_fixture_skip_never_hides_invalid_ci_provenance(
    monkeypatch: pytest.MonkeyPatch, github: str, ci: str
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", github)
    monkeypatch.setenv("CI", ci)
    engine = _guard_engine("daimon_test")  # no builder provenance
    if github == "true" and ci == "true":
        with pytest.raises(ValueError, match="owned"):
            _require_hook_sm(engine)
    else:
        with pytest.raises(pytest.skip.Exception, match="plain local"):
            _require_hook_sm(engine)


def _member(harness: DiscordSurfaceHarness) -> None:
    guild = harness.thread.guild
    state = harness.client._connection  # pyright: ignore[reportPrivateUsage]
    # Real Discord models/permission calculation. No mocked poster/send call.
    guild._roles[100] = discord.Role(  # pyright: ignore[reportPrivateUsage]
        guild=guild,
        state=state,
        data=cast(Any, {"id": "100", "name": "@everyone", "permissions": "8"}),
    )
    guild._members[103] = discord.Member(  # pyright: ignore[reportPrivateUsage]
        guild=guild,
        state=state,
        data=cast(
            Any,
            {
                "user": {"id": "103", "username": "qa", "discriminator": "0", "avatar": None},
                "roles": [],
                "flags": 0,
            },
        ),
    )


async def _seed(
    session: AsyncSession, *, destination: bool = True, enabled: bool = True
) -> RoutineRow:
    tenant = await make_tenant(session, workspace_id="100")
    await set_access_policy(session, tenant_id=tenant.id, policy=TenantAccessPolicy())
    row = await create_routine(
        session,
        tenant_id=tenant.id,
        created_by_user_id="103",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="ROUTINE-PING",
        next_fire_at=_ANCHOR + timedelta(seconds=60),
        destination_kind="thread" if destination else None,
        destination_id="102" if destination else None,
        enabled=enabled,
    )
    await session.commit()
    return row


async def _bound(
    sm: async_sessionmaker[AsyncSession],
    row: RoutineRow,
    *,
    fail: bool = False,
) -> tuple[ClockedRoutineJobs, list[float], DiscordSurfaceHarness]:
    clock = RoutineJobClock(anchor=_ANCHOR)
    harness = DiscordSurfaceHarness(
        evidence_id="routine-jobs",
        session_id="test-session",
        root_turn_id="test-root",
        clock=clock,
    )
    _member(harness)
    fired: list[float] = []

    async def fire(due: RoutineRow) -> None:
        # Synthetic fire ONLY tests orchestration. Actual scheduler/DB/adapter
        # stay real. This does NOT exercise or certify the native factory A6.
        fired.append(clock())
        if fail:
            raise RuntimeError("scheduled execution failed")
        async with sm() as session, session.begin():
            await record_result(
                session,
                due.id,
                tail="ROUTINE-PING" + "x" * 2500,
                error=None,
                delivery="pending" if due.destination_kind else None,
            )

    async def fetch(channel_id: int) -> object:
        result = harness.client.get_channel(channel_id)
        if result is None:
            raise LookupError("offline channel absent")
        return result

    async def dm(guild_id: int, user_id: int) -> discord.abc.Messageable:
        raise AssertionError("unexpected DM fallback")

    post = make_discord_routine_poster(
        sm,
        fetch_channel=fetch,
        open_dm=dm,
        dm_policy=lambda _: DirectMessagePolicy(),
    )
    jobs = ClockedRoutineJobs(
        sessionmaker=sm,
        clock=clock,
        caps=NoCaps(),
        fire=fire,
        post=post,
        gateway=harness.gateway,
        bindings=(RoutineBinding(row.tenant_id, row.id),),
    )
    return jobs, fired, harness


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (150.0, [60.0, 120.0]),
        (240.0, [60.0, 120.0, 180.0, 240.0]),
        (1920.0, [float(x) for x in range(60, 1921, 60)]),
    ],
)
async def test_long_wait_runs_real_ticks_and_adapter_delivery(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    seconds: float,
    expected: list[float],
) -> None:
    row = await _seed(db_session)
    jobs, fired, harness = await _bound(owned_sm, row)
    receipt = await jobs.advance(seconds)
    assert fired == expected  # jumping directly to the end misses these slots
    assert [tick.at_s for tick in receipt.ticks] == [
        float(x) for x in range(0, int(seconds) + 1, 15)
    ]
    events = [event for tick in receipt.ticks for event in tick.posts]
    # Main 656f0ecd13698c1ec3ea77157db55b08f877d15d: three full-reply chunks per fire.
    assert len(events) == 3 * len(expected)
    assert [event.message.observed_s for event in events] == [
        at for at in expected for _ in range(3)
    ]
    assert all(event.operation == "send" and len(event.message.content) <= 2000 for event in events)
    assert [event.message.content for event in events] == _EXPECTED_DELIVERY_CHUNKS * len(expected)
    assert len(harness.gateway.events) == 3 * len(expected)
    for tick in receipt.ticks:
        state = tick.readbacks[0]
        assert state.destination == "saved_destination"
        if tick.posts:
            assert state.row.last_fired_at == tick.at_utc
            assert state.row.delivered_at == tick.at_utc
            assert state.row.delivery_status == "delivered"
        assert state.row.last_skip_reason is None
    assert receipt.finished_s == seconds


async def test_no_destination_is_real_record_only_not_an_invented_warning(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    row = await _seed(db_session, destination=False)
    jobs, fired, harness = await _bound(owned_sm, row)
    receipt = await jobs.advance(150.0)
    assert fired == [60.0, 120.0]
    assert not harness.gateway.events
    after = receipt.ticks[-1].readbacks[0]
    assert after.destination == "no_destination"
    assert after.row.last_result_tail is not None
    assert after.row.delivery_status is None and after.row.delivered_at is None
    # NEW23's warning/post assertion remains unproved by this job receipt.


async def test_failure_is_actual_scheduler_error_readback_not_success_delivery(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    row = await _seed(db_session)
    jobs, fired, harness = await _bound(owned_sm, row, fail=True)
    receipt = await jobs.advance(150.0)
    assert fired == [60.0, 120.0]
    assert (
        receipt.ticks[-1].readbacks[0].row.last_error == "RuntimeError: scheduled execution failed"
    )
    # Main #835 (8500fd7634b10dd5f454c934a3172918d8fb6375) posts the first
    # consecutive failure once through the real adapter, without a result.
    assert len(harness.gateway.events) == 1
    event = harness.gateway.events[0]
    assert event.operation == "send"
    assert event.message.channel_id == "102"
    assert event.message.observed_s == 60.0
    assert event.message.content == (
        "daimon's routine (* * * * *, UTC) failed, so there's no result this time.\n\n"
        "Something went wrong while it was running.\n\n"
        "It will try again at its next scheduled time."
    )
    posts = [post for tick in receipt.ticks for post in tick.posts]
    assert posts == harness.gateway.events
    after = receipt.ticks[-1].readbacks[0]
    assert after.destination == "saved_destination"
    assert after.row.last_result_tail is None
    assert after.row.delivery_status == "delivered"
    # Successful notice settlement clears the outbox note, not last_error.
    assert after.row.delivery_note is None
    assert after.row.delivered_at == _ANCHOR + timedelta(seconds=60)


async def test_disabled_routine_never_fires(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    row = await _seed(db_session, enabled=False)
    jobs, fired, harness = await _bound(owned_sm, row)
    await jobs.advance(240.0)
    assert not fired and not harness.gateway.events


async def test_default_poller_still_settles_at_real_wall_time(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    row = await _seed(db_session)
    async with owned_sm() as session, session.begin():
        await record_result(session, row.id, tail="default path", error=None, delivery="pending")
    jobs, _, harness = await _bound(owned_sm, row)
    before = datetime.now(UTC)
    count = await poll_deliveries_once(owned_sm, platform="discord", post=jobs._post, now=_ANCHOR)  # pyright: ignore[reportPrivateUsage]
    after = datetime.now(UTC)
    async with owned_sm() as session:
        read = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert count == 1 and len(harness.gateway.events) == 1
    assert read is not None and read.delivered_at is not None
    assert before <= read.delivered_at <= after
    assert read.delivered_at != _ANCHOR


@pytest.mark.parametrize("database", ["daimon", "daimon_test", "daimon_test_nc", "operator_test"])
def test_foreign_main_bind_refuses_without_connection(database: str) -> None:
    engine = _guard_engine(database)
    with pytest.raises(ValueError, match="owned"):
        _runner(async_sessionmaker(engine))


async def test_new_foreign_routed_bind_refuses_before_clock_move_or_connect() -> None:
    own = _guard_engine("daimon_test_nc_N5")
    foreign = _guard_engine("operator_test")
    sm = async_sessionmaker(own)
    jobs = _runner(sm)
    sm.configure(binds={Routine: foreign})
    with pytest.raises(ValueError, match="owned"):
        await jobs.advance(150.0)
    assert jobs.clock() == 0.0
    await own.dispose()
    await foreign.dispose()


async def test_missing_credential_expiry_refuses_without_connection_or_time_skip() -> None:
    engine = _guard_engine("daimon_test_nc_N5")
    jobs = _runner(async_sessionmaker(engine))
    with pytest.raises(LongWaitUnavailable) as caught:
        await jobs.advance(1920.0, job="credential_form_expiry")
    assert caught.value.code == "CREDENTIAL_FORM_EXPIRY_JOB_UNAVAILABLE"
    assert jobs.clock() == 0.0
    await engine.dispose()


@pytest.mark.parametrize("seconds", [-1.0, float("nan"), float("inf"), 1921.0])
async def test_invalid_wait_refuses_before_connection(seconds: float) -> None:
    engine = _guard_engine("daimon_test_nc_N5")
    jobs = _runner(async_sessionmaker(engine))
    with pytest.raises(ValueError, match="finite"):
        await jobs.advance(seconds)
    assert jobs.clock() == 0.0
    await engine.dispose()


@pytest.mark.parametrize(
    "anchor",
    [
        datetime(2024, 1, 1),
        datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_clock_requires_utc(anchor: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        RoutineJobClock(anchor=anchor)


async def test_repeated_advances_do_not_skip_or_repeat_slots(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    row = await _seed(db_session)
    jobs, fired, harness = await _bound(owned_sm, row)
    first = await jobs.advance(150.0)
    second = await jobs.advance(90.0)
    assert fired == [60.0, 120.0, 180.0, 240.0]
    assert (first.finished_s, second.started_s, second.finished_s) == (150.0, 150.0, 240.0)
    # Main 656f0ecd13698c1ec3ea77157db55b08f877d15d: all three chunks share their fire's time.
    assert [event.message.observed_s for event in harness.gateway.events] == [
        at for at in fired for _ in range(3)
    ]
    assert [event.message.content for event in harness.gateway.events] == (
        _EXPECTED_DELIVERY_CHUNKS * len(fired)
    )


async def test_failed_actual_adapter_post_is_skipped_and_never_retried(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    row = await _seed(db_session)
    jobs, fired, harness = await _bound(owned_sm, row)
    harness.gateway.fail_next("send", RuntimeError("Discord send rejected"))
    receipt = await jobs.advance(150.0)
    failed = next(tick for tick in receipt.ticks if tick.at_s == 60.0)
    assert failed.delivery_claims == 1 and not failed.posts
    assert failed.readbacks[0].row.delivery_status == "skipped"
    assert failed.readbacks[0].row.delivery_note == "post_failed"
    assert fired == [60.0, 120.0]
    # Main 656f0ecd13698c1ec3ea77157db55b08f877d15d: only the next fire's three chunks post.
    assert [event.message.observed_s for event in harness.gateway.events] == [120.0] * 3
    assert [event.message.content for event in harness.gateway.events] == _EXPECTED_DELIVERY_CHUNKS


async def test_foreign_tenant_binding_refuses_before_time_move(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    row = await _seed(db_session)
    jobs, fired, harness = await _bound(owned_sm, row)
    jobs._bindings = (RoutineBinding(uuid.uuid4(), row.id),)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ValueError, match="another tenant"):
        await jobs.advance(150.0)
    assert jobs.clock() == 0.0 and not fired and not harness.gateway.events


async def test_pending_fire_blocks_clock_advance_and_cancellation_joins_job(
    owned_sm: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    row = await _seed(db_session)
    jobs, _, harness = await _bound(owned_sm, row)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_fire(due: RoutineRow) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    jobs._fire = blocked_fire  # pyright: ignore[reportPrivateUsage]
    task = asyncio.create_task(jobs.advance(150.0))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert jobs.clock() == 60.0
        with pytest.raises(ValueError, match="already running"):
            await jobs.advance(240.0)
        assert jobs.clock() == 60.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert cancelled.is_set() and not harness.gateway.events
    async with owned_sm() as session:
        after = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.last_error == "scheduler_shutdown"
    assert jobs.clock() == 60.0  # no completed 150s receipt manufactured
