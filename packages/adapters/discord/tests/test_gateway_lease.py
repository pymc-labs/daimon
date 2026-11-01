"""The Discord gateway lease decides which overlapping process may connect."""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import asynccontextmanager
from dataclasses import replace
from secrets import randbits
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.__main__ import connect_gateway, start_when_leased
from daimon.adapters.discord.runtime import DiscordRuntime, hold_worker_owner
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.discord_gateway_lease import GatewayLease
from daimon.core.stores.worker_ownership import OWNER_LOSS_GRACE_S
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.turn_fakes import deps_for
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker

from .harness import make_bot


@pytest.fixture
def lock_key() -> int:
    """Advisory locks are database-wide, even with separate xdist schemas."""
    return randbits(30)


@asynccontextmanager
async def _process(engine: AsyncEngine, lock_key: int, **kwargs: Any):
    """One Discord process's owner connection and gateway lease."""
    lost = asyncio.Event()
    owner_key = randbits(30)
    async with hold_worker_owner(
        engine,
        owner_key=owner_key,
        ownership_lost=lost,
        gateway_lock_key=lock_key,
        **kwargs,
    ) as lease:
        yield lease, lost


async def _row(engine: AsyncEngine) -> Any:
    async with async_sessionmaker(engine)() as session:
        return (
            await session.execute(
                text("""SELECT holder_owner_key, released_at FROM discord_gateway_lease
                     WHERE id = 1""")
            )
        ).first()


async def _lock_granted(engine: AsyncEngine, lock_key: int) -> bool:
    async with async_sessionmaker(engine)() as session:
        return bool(
            await session.scalar(
                text("""SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
                     AND granted AND classid = 0 AND objid = :key AND objsubid = 1)"""),
                {"key": lock_key},
            )
        )


async def _reset(engine: AsyncEngine) -> None:
    async with async_sessionmaker(engine)() as session:
        await session.execute(text("DELETE FROM discord_gateway_lease"))
        # Earlier tests that fail-stop leave heartbeats behind, as production does.
        await session.execute(text("DELETE FROM discord_worker_heartbeats"))
        await session.commit()


async def test_first_process_takes_the_lease_immediately(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    await _reset(db_nullpool_engine)
    async with _process(db_nullpool_engine, lock_key) as (lease, _):
        waited_ms = await asyncio.wait_for(lease.acquire(asyncio.Event()), timeout=3)
        assert waited_ms < 1000
        assert lease.held
        row = await _row(db_nullpool_engine)
        assert row.holder_owner_key == lease.owner_key
        assert row.released_at is None
        await lease.release()
        row = await _row(db_nullpool_engine)
        assert row.holder_owner_key == lease.owner_key
        assert row.released_at is not None
        assert not lease.held


async def test_second_process_waits_for_a_clean_release_then_goes_at_once(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    await _reset(db_nullpool_engine)
    async with (
        _process(db_nullpool_engine, lock_key) as (old, _),
        _process(db_nullpool_engine, lock_key) as (new, _),
    ):
        await old.acquire(asyncio.Event())
        waiter = asyncio.create_task(new.acquire(asyncio.Event()))
        await asyncio.sleep(0.6)
        assert not waiter.done(), "the standby must not connect while the old holder holds"
        assert not new.held

        await old.release()

        # The old process's owner connection is still alive: a clean release
        # must not make the successor wait out its liveness.
        waited_ms = await asyncio.wait_for(waiter, timeout=2)
        assert waited_ms >= 500
        assert new.held
        assert (await _row(db_nullpool_engine)).holder_owner_key == new.owner_key
        await new.release()


async def test_unclean_holder_loss_makes_the_successor_wait_for_liveness_to_expire(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    await _reset(db_nullpool_engine)
    dead_key = randbits(30)
    factory = async_sessionmaker(db_nullpool_engine)
    async with factory() as session:
        # The previous holder's connection died (its lock is gone) without a
        # release, but it may still be connected to Discord until it fails stop.
        await session.execute(
            text("""INSERT INTO discord_gateway_lease
                 (id, holder_owner_key, acquired_at, released_at)
                 VALUES (1, :key, clock_timestamp(), NULL)"""),
            {"key": dead_key},
        )
        await session.execute(
            text("""INSERT INTO discord_worker_heartbeats (owner_key, seen_at)
                 VALUES (:key, clock_timestamp())"""),
            {"key": dead_key},
        )
        await session.commit()
    try:
        async with _process(db_nullpool_engine, lock_key) as (new, _):
            waiter = asyncio.create_task(new.acquire(asyncio.Event()))
            await asyncio.sleep(0.8)
            assert not waiter.done(), "the lock is free but the old holder may still be connected"
            assert (await _row(db_nullpool_engine)).holder_owner_key == dead_key

            async with factory() as session:
                await session.execute(
                    text("""UPDATE discord_worker_heartbeats
                         SET seen_at = clock_timestamp() - (:age * interval '1 second')
                         WHERE owner_key = :key"""),
                    {"key": dead_key, "age": OWNER_LOSS_GRACE_S + 1},
                )
                await session.commit()
            await asyncio.wait_for(waiter, timeout=3)
            assert new.held
            assert (await _row(db_nullpool_engine)).holder_owner_key == new.owner_key
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM discord_worker_heartbeats WHERE owner_key = :key"),
                {"key": dead_key},
            )
            await session.commit()


async def test_cancelled_wait_leaves_the_lease_alone(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    await _reset(db_nullpool_engine)
    dead_key = randbits(30)
    factory = async_sessionmaker(db_nullpool_engine)
    async with factory() as session:
        await session.execute(
            text("""INSERT INTO discord_gateway_lease
                 (id, holder_owner_key, acquired_at, released_at)
                 VALUES (1, :key, clock_timestamp(), NULL)"""),
            {"key": dead_key},
        )
        await session.execute(
            text("""INSERT INTO discord_worker_heartbeats (owner_key, seen_at)
                 VALUES (:key, clock_timestamp())"""),
            {"key": dead_key},
        )
        await session.commit()
    try:
        async with _process(db_nullpool_engine, lock_key) as (new, _):
            waiter = asyncio.create_task(new.acquire(asyncio.Event()))
            await asyncio.sleep(0.6)
            assert await _lock_granted(db_nullpool_engine, lock_key)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            assert not new.held
            assert not await _lock_granted(db_nullpool_engine, lock_key)
            assert (await _row(db_nullpool_engine)).holder_owner_key == dead_key
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM discord_worker_heartbeats WHERE owner_key = :key"),
                {"key": dead_key},
            )
            await session.commit()


async def _kill_owner_backend(engine: AsyncEngine, owner_key: int) -> None:
    async with async_sessionmaker(engine)() as session:
        pid = await session.scalar(
            text("""SELECT pid FROM pg_locks
                 WHERE locktype = 'advisory' AND granted AND objsubid = 1
                   AND classid = 0 AND objid = :key"""),
            {"key": owner_key},
        )
        assert pid is not None
        await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        await session.commit()
        # Termination signals the backend before shutdown releases its locks.
        # Synchronize with that shutdown, not with a retry of the thief's take.
        async with asyncio.timeout(3):
            while await session.scalar(
                text("""SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = :pid)
                     OR EXISTS (SELECT 1 FROM pg_locks WHERE pid = :pid)"""),
                {"pid": pid},
            ):
                # A new transaction refreshes PostgreSQL's activity snapshot.
                await session.commit()
                await asyncio.sleep(0.01)


async def test_holder_keeps_the_lease_across_a_brief_connection_loss(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    await _reset(db_nullpool_engine)
    async with _process(db_nullpool_engine, lock_key, probe_interval_s=0.2) as (lease, lost):
        await lease.acquire(asyncio.Event())
        await _kill_owner_backend(db_nullpool_engine, lease.owner_key)
        await asyncio.sleep(1.5)
        assert not lost.is_set()
        assert await _lock_granted(db_nullpool_engine, lock_key)
        await lease.release()


async def test_lease_taken_during_a_connection_loss_fails_the_old_holder_stop(
    db_nullpool_engine: AsyncEngine, lock_key: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    thief_acquired = asyncio.Event()
    original_confirm = GatewayLease.confirm_after_reconnect

    async def confirm_after_thief(lease: GatewayLease, connection: AsyncConnection) -> bool:
        # Choose the loss/takeover interleaving before the real lease check.
        await thief_acquired.wait()
        return await original_confirm(lease, connection)

    monkeypatch.setattr(GatewayLease, "confirm_after_reconnect", confirm_after_thief)
    await _reset(db_nullpool_engine)
    async with _process(db_nullpool_engine, lock_key, probe_interval_s=0.3) as (lease, lost):
        await lease.acquire(asyncio.Event())
        await _kill_owner_backend(db_nullpool_engine, lease.owner_key)
        async with db_nullpool_engine.connect() as thief:
            assert await thief.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
            await thief.commit()
            thief_acquired.set()
            await asyncio.wait_for(lost.wait(), timeout=5)


def _fake_lease(events: list[str], gate: asyncio.Event | None = None) -> GatewayLease:
    lease = MagicMock(spec=GatewayLease)

    async def acquire(_lost: asyncio.Event) -> int:
        events.append("acquire_begin")
        if gate is not None:
            await gate.wait()
        events.append("acquired")
        return 0

    async def release() -> None:
        events.append("release")

    lease.acquire = acquire
    lease.release = AsyncMock(side_effect=release)
    return cast(GatewayLease, lease)


def _runtime(lease: GatewayLease) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    settings.defaults_root = MagicMock()
    settings.discord.max_concurrent_turns_per_tenant = 3
    settings.discord.thread_open_notice_after_s = 3.0
    return DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=MagicMock(spec=async_sessionmaker),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None),
        gateway_lease=lease,
    )


async def test_bot_does_not_start_before_the_lease_is_held() -> None:
    events: list[str] = []
    gate = asyncio.Event()
    bot = MagicMock()
    bot.runtime = _runtime(_fake_lease(events, gate))

    async def record_start(_token: str) -> None:
        events.append("start")

    bot.start = AsyncMock(side_effect=record_start)
    bot.is_closed = MagicMock(return_value=False)
    started = asyncio.Event()
    task = asyncio.create_task(
        start_when_leased(bot, "token", stop=asyncio.Event(), started=started)
    )
    await asyncio.sleep(0.1)
    bot.start.assert_not_called()
    assert not started.is_set()
    gate.set()
    await asyncio.wait_for(task, timeout=2)
    assert events == ["acquire_begin", "acquired", "start"]


async def test_stop_while_waiting_exits_without_starting_or_touching_the_lease() -> None:
    events: list[str] = []
    bot = MagicMock()
    bot.runtime = _runtime(_fake_lease(events, asyncio.Event()))
    bot.start = AsyncMock()
    stop, started = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(start_when_leased(bot, "token", stop=stop, started=started))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    bot.start.assert_not_called()
    bot.runtime.gateway_lease.release.assert_not_called()  # pyright: ignore[reportAttributeAccessIssue,reportOptionalMemberAccess]
    assert not started.is_set()


def _started_bot(events: list[str], *, closes: bool = True) -> MagicMock:
    bot = MagicMock()
    bot.runtime = _runtime(_fake_lease(events))
    closed = False

    async def run(_token: str) -> None:
        nonlocal closed
        events.append("start")
        closed = closes
        events.append("start_returned")

    bot.start = AsyncMock(side_effect=run)
    bot.is_closed = MagicMock(side_effect=lambda: closed)
    bot.close = AsyncMock()
    return bot


async def test_lease_is_released_when_start_returns_after_a_clean_close() -> None:
    """In the main task, before the runtime tears down the owner connection."""
    events: list[str] = []
    bot = _started_bot(events)
    await start_when_leased(bot, "token", stop=asyncio.Event(), started=asyncio.Event())
    assert events == ["acquire_begin", "acquired", "start", "start_returned", "release"]


async def test_lease_waits_for_an_in_progress_close_to_finish() -> None:
    """is_closed() is true once closing starts; start() may return before it ends."""
    events: list[str] = []
    bot = _started_bot(events)
    gate = asyncio.Event()

    async def close() -> None:
        await gate.wait()
        events.append("closed")

    bot.close = AsyncMock(side_effect=close)
    task = asyncio.create_task(
        start_when_leased(bot, "token", stop=asyncio.Event(), started=asyncio.Event())
    )
    await asyncio.sleep(0.1)
    assert "release" not in events
    gate.set()
    await asyncio.wait_for(task, timeout=2)
    assert events[-2:] == ["closed", "release"]


async def test_failed_close_does_not_release_the_lease() -> None:
    events: list[str] = []
    bot = _started_bot(events)
    bot.close = AsyncMock(side_effect=OSError("close frame not sent"))
    await start_when_leased(bot, "token", stop=asyncio.Event(), started=asyncio.Event())
    assert "release" not in events


async def test_drain_does_not_release_the_lease_itself() -> None:
    """The drain task racing the runtime teardown on the owner connection failed on staging."""
    events: list[str] = []
    bot = make_bot(_runtime(_fake_lease(events)))
    bot.close = AsyncMock(side_effect=lambda: events.append("close"))  # type: ignore[method-assign]
    await bot._drain_and_close()  # pyright: ignore[reportPrivateUsage]
    assert events == ["close"]


async def test_gateway_still_open_does_not_release_the_lease() -> None:
    events: list[str] = []
    bot = _started_bot(events, closes=False)
    await start_when_leased(bot, "token", stop=asyncio.Event(), started=asyncio.Event())
    assert "release" not in events


async def test_ownership_loss_does_not_release_the_lease() -> None:
    events: list[str] = []
    bot = _started_bot(events)
    bot.runtime.ownership_lost.set()
    await start_when_leased(bot, "token", stop=asyncio.Event(), started=asyncio.Event())
    assert "release" not in events


def test_runtime_fields_include_the_lease() -> None:
    assert "gateway_lease" in {f.name for f in dataclasses.fields(DiscordRuntime)}


async def test_empty_lease_row_waits_for_an_unrecorded_live_owner(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    """A process from before the lease is connected without ever recording itself."""
    await _reset(db_nullpool_engine)
    legacy_key = randbits(30)
    factory = async_sessionmaker(db_nullpool_engine)
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO discord_worker_heartbeats (owner_key, seen_at)"
                " VALUES (:key, clock_timestamp())"
            ),
            {"key": legacy_key},
        )
        await session.commit()
    try:
        async with _process(db_nullpool_engine, lock_key) as (new, _):
            waiter = asyncio.create_task(new.acquire(asyncio.Event()))
            await asyncio.sleep(0.8)
            assert not waiter.done(), "the legacy process is still connected"
            async with factory() as session:  # it exits gracefully: its row goes
                await session.execute(
                    text("DELETE FROM discord_worker_heartbeats WHERE owner_key = :key"),
                    {"key": legacy_key},
                )
                await session.commit()
            await asyncio.wait_for(waiter, timeout=3)
            assert new.held
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM discord_worker_heartbeats WHERE owner_key = :key"),
                {"key": legacy_key},
            )
            await session.commit()


async def test_recorded_lease_still_waits_for_a_live_pre_lease_owner(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    """After a rollback to pre-lease code, a cleanly released row is left behind."""
    await _reset(db_nullpool_engine)
    legacy_key = randbits(30)
    factory = async_sessionmaker(db_nullpool_engine)
    async with factory() as session:
        await session.execute(
            text("""INSERT INTO discord_gateway_lease
                 (id, holder_owner_key, acquired_at, released_at)
                 VALUES (1, :key, clock_timestamp(), clock_timestamp())"""),
            {"key": randbits(30)},
        )
        await session.execute(
            text(
                "INSERT INTO discord_worker_heartbeats (owner_key, seen_at)"
                " VALUES (:key, clock_timestamp())"
            ),
            {"key": legacy_key},
        )
        await session.commit()
    try:
        async with _process(db_nullpool_engine, lock_key) as (new, _):
            waiter = asyncio.create_task(new.acquire(asyncio.Event()))
            await asyncio.sleep(0.8)
            assert not waiter.done(), "the rolled-back process is still connected"
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
    finally:
        async with factory() as session:
            await session.execute(
                text("DELETE FROM discord_worker_heartbeats WHERE owner_key = :key"),
                {"key": legacy_key},
            )
            await session.commit()


async def test_own_unreleased_row_is_retaken_at_once(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    """A take that committed but lost its acknowledgement must not wait on itself."""
    await _reset(db_nullpool_engine)
    async with _process(db_nullpool_engine, lock_key) as (lease, _):
        async with async_sessionmaker(db_nullpool_engine)() as session:
            await session.execute(
                text("""INSERT INTO discord_gateway_lease
                     (id, holder_owner_key, acquired_at, released_at)
                     VALUES (1, :key, clock_timestamp(), NULL)"""),
                {"key": lease.owner_key},
            )
            await session.commit()
        await asyncio.wait_for(lease.acquire(asyncio.Event()), timeout=2)
        assert lease.held


async def test_leaving_the_runtime_without_release_frees_the_successor_at_once(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    """bot.start raising or any runtime exit: the heartbeat row goes, so no liveness wait."""
    await _reset(db_nullpool_engine)
    async with _process(db_nullpool_engine, lock_key) as (old, _):
        await old.acquire(asyncio.Event())
    async with _process(db_nullpool_engine, lock_key) as (new, _):
        waited_ms = await asyncio.wait_for(new.acquire(asyncio.Event()), timeout=2)
        assert waited_ms < 1000


async def test_taking_the_lease_overwrites_an_earlier_holders_clean_release(
    db_nullpool_engine: AsyncEngine, lock_key: int
) -> None:
    """A stale released_at must not let a third process connect beside the holder."""
    await _reset(db_nullpool_engine)
    factory = async_sessionmaker(db_nullpool_engine)
    async with factory() as session:
        await session.execute(
            text("""INSERT INTO discord_gateway_lease
                 (id, holder_owner_key, acquired_at, released_at)
                 VALUES (1, :key, clock_timestamp(), clock_timestamp())"""),
            {"key": randbits(30)},
        )
        await session.commit()
    # A long probe interval keeps A from reconnecting after its connection dies.
    async with (
        _process(db_nullpool_engine, lock_key, probe_interval_s=60) as (first, _),
        _process(db_nullpool_engine, lock_key) as (third, _),
    ):
        await first.acquire(asyncio.Event())
        row = await _row(db_nullpool_engine)
        assert row.holder_owner_key == first.owner_key
        assert row.released_at is None

        # The first holder's connection dies uncleanly; it may still be on Discord.
        await _kill_owner_backend(db_nullpool_engine, first.owner_key)
        waiter = asyncio.create_task(third.acquire(asyncio.Event()))
        await asyncio.sleep(0.8)
        assert not waiter.done()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def test_heartbeat_grace_outlasts_the_holders_fail_stop_bound() -> None:
    """A successor waits out OWNER_LOSS_GRACE_S; the old holder must have stopped by then."""
    from daimon.core.stores.worker_ownership import (
        OWNER_HARD_EXIT_S,
        OWNER_PROBE_INTERVAL_S,
        OWNER_PROBE_TIMEOUT_S,
        OWNER_RETRY_BUDGET_S,
    )

    fail_stop_bound = (
        OWNER_PROBE_INTERVAL_S + OWNER_PROBE_TIMEOUT_S + OWNER_RETRY_BUDGET_S + OWNER_HARD_EXIT_S
    )
    assert fail_stop_bound < OWNER_LOSS_GRACE_S


def _intents_bot(*, members: bool) -> MagicMock:
    bot = MagicMock()
    bot.intents = discord.Intents.default()
    bot.intents.members = members
    bot.start = AsyncMock(side_effect=discord.PrivilegedIntentsRequired(None))
    bot.connect = AsyncMock()
    return bot


async def test_without_the_members_intent_the_bot_connects_without_it() -> None:
    """The Server Members intent is optional: refused, the bot connects again without it
    and member lists stay unloaded."""
    bot = _intents_bot(members=True)

    await connect_gateway(bot, "token")

    bot.connect.assert_awaited_once()
    assert bot._connection._intents.members is False, "identifies again without members"
    assert bot._connection._intents.message_content == bot.intents.message_content


async def test_a_refusal_without_the_members_intent_still_fails() -> None:
    """Message Content cannot be dropped: the bot could not read mentions."""
    bot = _intents_bot(members=False)

    with pytest.raises(discord.PrivilegedIntentsRequired):
        await connect_gateway(bot, "token")
    bot.connect.assert_not_awaited()
