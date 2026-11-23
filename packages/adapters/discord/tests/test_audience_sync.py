"""Room facts come from one REST reconcile per server and fail closed: unknown whenever a
change, an outage or a stuck read means they cannot be vouched for. Discord is a fake
(`audience_fakes.RestDiscord`); the clock is simulated and real waits are milliseconds."""

from __future__ import annotations

import asyncio
import types
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.discord import audience_sync
from daimon.adapters.discord.audience_sync import AudienceSync
from daimon.adapters.discord.member_sync import tenant_of
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.stores import audience_facts as store
from daimon.core.stores.access_policy import set_access_policy
from daimon.testing.factories import make_tenant
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from . import audience_fakes as fake

START = datetime(2026, 5, 1, tzinfo=UTC)
TENANT = tenant_of(fake.GUILD)
ROLES = (str(fake.CORE), str(fake.FRIEND))


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


async def _setup(
    session: AsyncSession,
    *,
    internal: tuple[str, ...] = ROLES,
    team: tuple[str, ...] = (str(fake.CORE),),
    guild_id: int = fake.GUILD,
) -> None:
    await make_tenant(session, platform="discord", workspace_id=str(guild_id))
    await set_access_policy(
        session,
        tenant_id=tenant_of(guild_id),
        policy=TenantAccessPolicy(internal_role_ids=internal, team_role_ids=team),
    )
    await session.commit()


async def _fact(
    factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    channel_id: str,
    *,
    guild_id: int = fake.GUILD,
) -> RoomFacts:
    async with factory() as session:
        return await store.read_channel_facts(
            session, tenant_id=tenant_of(guild_id), channel_id=channel_id, now=clock.now
        )


async def _status(
    factory: async_sessionmaker[AsyncSession], clock: Clock, guild_id: int = fake.GUILD
) -> store.AudienceStatus | None:
    async with factory() as session:
        return await store.read_status(session, tenant_id=tenant_of(guild_id), now=clock.now)


class World:
    """One fictional server, Discord's REST API for it, and the bot's view of it."""

    def __init__(self, *guild_ids: int) -> None:
        self.servers = [fake.RestServer(g) for g in (guild_ids or (fake.GUILD,))]
        self.server = self.servers[0]
        self.discord = fake.RestDiscord(*self.servers)
        self.guilds = [fake.gateway_guild(g) for g in (guild_ids or (fake.GUILD,))]
        self.guild = self.guilds[0]

    def sync(
        self,
        factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        reader: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AudienceSync:
        options: dict[str, Any] = {
            "clock": clock,
            "fresh_for_s": 180,
            "debounce_s": 0,
            "reconcile_every_s": 300,
            "reconcile_limit_s": 600,
            **kwargs,
        }
        return AudienceSync(factory, reader=self.discord.reader(**(reader or {})), **options)


def _gate(server: fake.RestServer, route: str = "members") -> asyncio.Event:
    """Hold every read of `route` until the event is set."""
    gate = asyncio.Event()

    async def wait(_request: httpx.Request) -> None:
        await gate.wait()

    server.hooks[route] = wait
    return gate


@pytest.fixture
async def pooled(
    db_clean: None, db_committing_engine: AsyncEngine
) -> async_sessionmaker[AsyncSession]:
    """Separate pooled connections, for tests that read the facts while the publisher
    writes in the background (the shared test connection takes one statement at a time)."""
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with factory() as session:
        await _setup(session)
    return factory


async def _eventually(check: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(1):
        while not await check():
            await asyncio.sleep(0.001)


async def _members_read(world: World, count: int) -> bool:
    return world.server.reads["members"] == count


def _open_room_one(server: fake.RestServer) -> None:
    """Discord now lets the outsider into room 1."""
    server.channels[0]["permission_overwrites"].append(
        fake.rest_overwrite(fake.OUTSIDER, member=True, allow=fake.VIEW_BIT)
    )


async def test_a_reconcile_publishes_what_discord_says_and_a_disconnect_withdraws_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)

    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "nothing before a read"
    assert await audience.reconcile(world.guild)
    assert await _fact(db_session_factory, clock, "1") == RoomFacts(False, False)
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False)
    assert await _fact(db_session_factory, clock, "3") == RoomFacts(False, True), "private thread"

    await audience.disconnected([world.guild])
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "gateway lost"
    await audience.resumed([world.guild])
    await audience.settle()
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "reconciled"


async def test_a_reconcile_corrects_a_missed_change_and_a_missed_leave(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()
    assert not (await _fact(db_session_factory, clock, "1")).contained

    _open_room_one(world.server)  # no event arrived
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained

    world.server.members = [m for m in world.server.members if m["user"]["id"] != "30"]
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])
    await audience.settle()
    assert not (await _fact(db_session_factory, clock, "2")).contained, "the leaver is gone"


async def test_renewal_is_bounded_by_the_read_start_and_a_stalled_discord_lapses(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()

    for _ in range(4):
        clock.now += timedelta(seconds=60)
        await audience.tick([world.guild])
        await audience.settle()
    assert (await _fact(db_session_factory, clock, "2")).contained, "renewed past 180 s"
    assert world.server.reads["guild"] == 1, "renewal asks Discord nothing"

    async def down(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    world.server.hooks["guild"] = down  # from the 300 s reconcile on
    for _ in range(5):
        clock.now += timedelta(seconds=60)
        await audience.tick([world.guild])
        await audience.settle()
    assert clock.now - START == timedelta(seconds=540)
    assert (await _fact(db_session_factory, clock, "2")).contained, "one failed reconcile is safe"

    clock.now += timedelta(seconds=60)  # 600 s after the last good read started
    await audience.tick([world.guild])
    await audience.settle()
    clock.now += timedelta(seconds=1)
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "limit reached"


async def test_a_read_that_started_more_than_the_limit_ago_is_rejected(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    slow = [601]

    async def slow_read(_request: httpx.Request) -> None:
        clock.now += timedelta(seconds=slow[0])

    world.server.hooks["members"] = slow_read
    with capture_logs() as logs:
        assert not await audience.reconcile(world.guild)
    assert await _status(db_session_factory, clock) is None, "nothing written"
    assert "discord.audience.read_too_old" in [entry["event"] for entry in logs]

    slow[0] = 500  # within the limit: published, but fresh only to its read start + 600 s
    started = clock.now
    assert await audience.reconcile(world.guild)
    status = await _status(db_session_factory, clock)
    assert status is not None and status.fresh_until == started + timedelta(seconds=600)


async def test_an_event_makes_the_whole_server_unknown_at_once_and_one_read_follows(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    await audience.tick([world.guild])
    await audience.settle()
    assert await _fact(pooled, clock, "1") == RoomFacts(False, False)

    gate = _gate(world.server)
    _open_room_one(world.server)
    for _ in range(5):
        audience.schedule(world.guild)

    async def withdrawn() -> bool:
        return await _fact(pooled, clock, "2") == UNKNOWN_FACTS

    await _eventually(withdrawn)
    assert await _fact(pooled, clock, "1") == UNKNOWN_FACTS, "every room"
    gate.set()
    await audience.settle()
    assert (await _fact(pooled, clock, "1")).contained
    assert world.server.reads["guild"] == 2, "five events share one read"


async def test_events_during_a_read_cross_it_and_one_more_read_follows(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()

    gate = _gate(world.server)
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])  # the periodic read starts and waits for members

    async def reading() -> bool:
        return world.server.reads["members"] == 2

    await _eventually(reading)
    _open_room_one(world.server)
    for _ in range(3):
        audience.schedule(world.guild)
    await asyncio.sleep(0.01)
    assert world.server.reads["members"] == 2, "one read at a time: none starts beside it"
    with capture_logs() as logs:
        gate.set()
        await audience.settle()

    assert (await _fact(db_session_factory, clock, "1")).contained
    assert [e["event"] for e in logs].count("discord.audience.read_crossed") == 1
    assert world.server.reads["guild"] == 3, "the crossed read, then one more for three events"


async def test_a_publish_crossed_by_an_event_is_retired(
    pooled: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event that lands while the facts are being written retires them right after."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    await audience.tick([world.guild])
    await audience.settle()
    real = store.publish_facts
    gate: list[asyncio.Event] = []

    async def publish_then_event(*args: Any, **kwargs: Any) -> int:
        generation = await real(*args, **kwargs)
        if not gate:
            gate.append(_gate(world.server))
            audience.schedule(world.guild)
        return generation

    monkeypatch.setattr(store, "publish_facts", publish_then_event)
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])

    async def withdrawn() -> bool:
        return bool(gate) and await _fact(pooled, clock, "1") == UNKNOWN_FACTS

    await _eventually(withdrawn)
    gate[0].set()
    await audience.settle()
    assert await _fact(pooled, clock, "1") == RoomFacts(False, False), "read again"


async def test_an_unavailable_server_stays_unknown_and_a_late_read_never_lands(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()

    gate = _gate(world.server)
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])

    async def reading() -> bool:
        return world.server.reads["members"] == 2

    await _eventually(reading)
    world.guild.unavailable = True
    await audience.mark_unknown(world.guild)
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS
    gate.set()
    await audience.settle()
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "the late read is void"

    clock.now += timedelta(seconds=60)
    await audience.tick([world.guild])
    await audience.settle()
    assert world.server.reads["guild"] == 2, "no read while it is unavailable"

    world.guild.unavailable = False
    audience.request_reconcile(world.guild)  # what the member reload does when it returns
    await audience.settle()
    assert await _fact(db_session_factory, clock, "1") == RoomFacts(False, False)

    # A short outage that ends before the read does: the server is available again when
    # the read finishes, but the read spans the outage, so its result is void.
    gate = _gate(world.server)
    audience.request_reconcile(world.guild)
    await _eventually(lambda: _members_read(world, 4))
    world.guild.unavailable = True
    await audience.mark_unknown(world.guild)
    world.guild.unavailable = False
    audience.request_reconcile(world.guild)
    with capture_logs() as logs:
        gate.set()
        await audience.settle()
    assert [e["event"] for e in logs].count("discord.audience.read_crossed") == 1
    assert world.server.reads["guild"] == 5, "the void read, then one after the outage"


async def test_a_read_crossing_a_disconnect_never_lands(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A disconnect cancels our own read, and its epoch voids one that ends anyway."""
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    gate = _gate(world.server)
    audience.request_reconcile(world.guild)

    async def reading() -> bool:
        return world.server.reads["members"] == 1

    await _eventually(reading)
    await audience.disconnected([world.guild])
    gate.set()
    await audience.settle()
    assert await _status(db_session_factory, clock) is None, "nothing published after it"


async def test_a_partial_member_scan_publishes_nothing_and_keeps_the_last_facts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    world.server.members += [fake.rest_member(1000 + n, fake.CORE) for n in range(1000)]
    audience = world.sync(db_session_factory, clock, reader={"attempts": 2})
    await audience.tick([world.guild])
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "2")).contained
    assert world.server.reads["members"] == 2, "1,008 members: two pages"

    async def second_page_down(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(503) if request.url.params["after"] != "0" else None

    world.server.hooks["members"] = second_page_down
    world.server.members = [m for m in world.server.members if m["user"]["id"] != "30"]
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "2")).contained, "page one alone is not used"


async def test_a_member_holding_a_role_the_role_list_lacks_publishes_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """discord.py drops a role it has not cached from `Member.roles`; a role that grants
    viewing would then vanish and the room read closed. The raw ids are checked instead."""
    await _setup(db_session)
    clock, world = Clock(), World()
    world.server.channels[0]["permission_overwrites"].append(
        fake.rest_overwrite(50, allow=fake.VIEW_BIT)
    )
    world.server.members[6] = fake.rest_member(fake.OUTSIDER, 50)
    audience = world.sync(db_session_factory, clock)

    assert not await audience.reconcile(world.guild)
    assert await _status(db_session_factory, clock) is None

    world.server.roles[50] = 0
    assert await audience.reconcile(world.guild)
    assert (await _fact(db_session_factory, clock, "1")).contained, "role 50 lets them in"


async def test_the_owner_sees_every_room(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    world.server.owner_id = fake.OUTSIDER
    world.server.channels[1]["permission_overwrites"] = fake.rest_internal_only()
    audience = world.sync(db_session_factory, clock)
    assert await audience.reconcile(world.guild)
    assert (await _fact(db_session_factory, clock, "1")).contained
    assert (await _fact(db_session_factory, clock, "2")).contained


@pytest.mark.usefixtures("db_clean")
async def test_one_stuck_server_does_not_stop_the_others_renewing(
    db_committing_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server two's reads hang and its renewal's database step blocks. Server one keeps
    renewing and reconciling; server two lapses; no tick waits for either."""
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with factory() as session:
        await _setup(session)
        await _setup(session, guild_id=4001)
    clock, world = Clock(), World(fake.GUILD, 4001)
    audience = world.sync(factory, clock, db_wait_s=0.05)
    await audience.tick(world.guilds)
    await audience.settle()
    assert (await _fact(factory, clock, "2", guild_id=4001)).contained

    _gate(world.servers[1], "guild")
    real = store.renew_fresh_until

    async def renew(session: AsyncSession, **kwargs: Any) -> bool:
        if kwargs["tenant_id"] == tenant_of(4001):
            await asyncio.Event().wait()
        return await real(session, **kwargs)

    monkeypatch.setattr(store, "renew_fresh_until", renew)
    loop = asyncio.get_running_loop()
    for _ in range(6):
        clock.now += timedelta(seconds=60)
        started = loop.time()
        await audience.tick(world.guilds)
        assert loop.time() - started < 0.5, "a tick never waits for a read"
        await asyncio.sleep(0.005)
    assert (await _fact(factory, clock, "2")).contained, "server one renewed throughout"
    assert world.servers[0].reads["guild"] == 2, "and got its 300 s reconcile"
    assert await _fact(factory, clock, "2", guild_id=4001) == UNKNOWN_FACTS, "two lapsed"

    started = loop.time()
    await audience.disconnected(world.guilds)  # cancels our own stuck read
    assert loop.time() - started < 0.5
    assert all(slot.task is None for slot in audience._slots.values())  # pyright: ignore[reportPrivateUsage]


async def test_renewal_starts_at_once_and_a_tick_never_waits_for_a_read(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock, renew_every_s=3600)
    assert await audience.reconcile(world.guild)
    _gate(world.server, "guild")
    audience.refresh(world.guild)  # a thread event: this read sticks
    clock.now += timedelta(seconds=120)

    running = asyncio.create_task(audience.run(lambda: [world.guild]))

    async def renewed() -> bool:
        status = await _status(pooled, clock)
        return status is not None and status.fresh_until == clock.now + timedelta(seconds=180)

    await _eventually(renewed)  # the first tick runs without waiting `renew_every_s`
    assert world.server.reads["guild"] == 2, "its reconcile started and is stuck"
    clock.now += timedelta(seconds=60)
    await asyncio.wait_for(audience.tick([world.guild]), 0.5)
    await _eventually(renewed)
    running.cancel()
    await audience.disconnected([world.guild])


async def test_a_failed_publish_on_reconnect_recovers_at_the_next_tick(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()
    await audience.disconnected([world.guild])

    real = store.publish_facts
    monkeypatch.setattr(store, "publish_facts", AsyncMock(side_effect=SQLAlchemyError("down")))
    await audience.resumed([world.guild])
    await audience.settle()
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS

    monkeypatch.setattr(store, "publish_facts", real)
    clock.now += timedelta(seconds=60)
    await audience.tick([world.guild])
    await audience.settle()
    assert await _fact(db_session_factory, clock, "1") == RoomFacts(False, False)


async def test_a_policy_set_or_changed_after_startup_is_picked_up_within_one_tick(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, internal=(), team=())
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)

    async def tick() -> None:
        clock.now += timedelta(seconds=60)
        await audience.tick([world.guild])
        await audience.settle()

    await tick()
    assert world.server.reads["guild"] == 0, "no internal roles: no read"
    assert await _status(db_session_factory, clock) is None

    await set_access_policy(
        db_session,
        tenant_id=TENANT,
        policy=TenantAccessPolicy(internal_role_ids=ROLES, team_role_ids=(str(fake.CORE),)),
    )
    await db_session.commit()
    await tick()
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "set"

    # The friend role stops being internal: the friend now counts as an outsider.
    await set_access_policy(
        db_session, tenant_id=TENANT, policy=TenantAccessPolicy(internal_role_ids=(str(fake.CORE),))
    )
    await db_session.commit()
    await tick()
    assert (await _fact(db_session_factory, clock, "1")).contained, "the friend is an outsider"

    await set_access_policy(
        db_session, tenant_id=TENANT, policy=TenantAccessPolicy(internal_role_ids=())
    )
    await db_session.commit()
    await tick()
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "retired"


async def test_losing_the_members_intent_marks_every_server_unknown(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    assert await world.sync(db_session_factory, clock).reconcile(world.guild)

    async with db_session_factory.begin() as session:
        await store.mark_all_unknown(session, now=clock.now)

    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS


async def test_the_policy_fence_is_held_from_the_roles_read_to_the_facts_written(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy write takes the same fence exclusively, so it cannot commit between the role
    lists the publisher read and the facts it writes from them. (The test connection is
    shared, so the order of the steps is checked, not the lock itself.)"""
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    steps: list[str] = []
    real_lock, real_load, real_publish = (
        audience_sync.lock_policy_writes_shared,
        audience_sync.load_access_policy,
        store.publish_facts,
    )

    async def lock(*args: Any, **kwargs: Any) -> None:
        steps.append("fence")
        await real_lock(*args, **kwargs)

    async def load(*args: Any, **kwargs: Any) -> TenantAccessPolicy:
        steps.append("roles")
        return await real_load(*args, **kwargs)

    async def publish(*args: Any, **kwargs: Any) -> int:
        steps.append("facts")
        return await real_publish(*args, **kwargs)

    monkeypatch.setattr(audience_sync, "lock_policy_writes_shared", lock)
    monkeypatch.setattr(audience_sync, "load_access_policy", load)
    monkeypatch.setattr(store, "publish_facts", publish)
    await audience.tick([world.guild])
    await audience.settle()
    assert steps[-3:] == ["fence", "roles", "facts"]


async def test_a_busy_fence_retires_the_facts_and_they_are_not_renewed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.session_fence_retry import FenceUnavailable

    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "published"

    real = audience_sync.lock_policy_writes_shared
    busy = [True]

    async def lock(*args: Any, **kwargs: Any) -> None:
        if busy[0]:
            raise FenceUnavailable(0.0, "advisory")
        await real(*args, **kwargs)

    monkeypatch.setattr(audience_sync, "lock_policy_writes_shared", lock)
    assert await audience.reconcile(world.guild) is False
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "retired at once"
    assert TENANT not in audience._published  # pyright: ignore[reportPrivateUsage]

    busy[0] = False
    clock.now += timedelta(seconds=60)
    await audience.tick([world.guild])
    await audience.settle()
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "recomputed"


async def test_role_lists_that_changed_before_the_publish_retire_the_facts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy write lands after the roles were read and before the publish takes the
    fence: the facts computed from the old lists are never published."""
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    real = audience_sync.lock_policy_writes_shared
    wrote = [False]

    async def lock(*args: Any, **kwargs: Any) -> None:
        if not wrote[0]:
            wrote[0] = True
            await set_access_policy(
                db_session,
                tenant_id=TENANT,
                policy=TenantAccessPolicy(internal_role_ids=(str(fake.CORE),)),
            )
            await db_session.commit()
        await real(*args, **kwargs)

    monkeypatch.setattr(audience_sync, "lock_policy_writes_shared", lock)
    await audience.tick([world.guild])
    await audience.settle()
    assert wrote[0]
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "old lists never publish"
    clock.now += timedelta(seconds=60)
    await audience.tick([world.guild])
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained, "the new lists decide"


async def test_a_channel_type_the_rule_does_not_know_does_not_stop_the_facts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    world.server.channels.append(
        {"id": "9", "type": 20, "name": "never logged", "permission_overwrites": []}
    )
    audience = world.sync(db_session_factory, clock)
    with capture_logs() as logs:
        assert await audience.reconcile(world.guild)

    assert await _fact(db_session_factory, clock, "1") == RoomFacts(False, False)
    assert await _fact(db_session_factory, clock, "9") == UNKNOWN_FACTS, "that room is unknown"
    assert "never logged" not in str(logs)


# --- the bot's handlers -----------------------------------------------------------------


class _Recorder:
    """Stands in for AudienceSync or MemberSync on a bot stub and records each call."""

    _SYNC = frozenset({"schedule", "refresh", "request_reconcile"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any) -> None:
            self.calls.append((name, args))

        def sync_call(*args: Any) -> None:
            self.calls.append((name, args))

        return sync_call if name in self._SYNC else call

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _bot_class() -> Any:
    from daimon.adapters.discord.bot import DaimonBot

    return DaimonBot


def _bot(
    *, allowed: bool = True, ready: bool = True, members_intent: bool = True
) -> types.SimpleNamespace:
    """The few attributes the audience handlers of DaimonBot read."""
    DaimonBot = _bot_class()
    guild = types.SimpleNamespace(id=1, members=[], channels=[], owner_id=7)
    bot = types.SimpleNamespace(
        intents=types.SimpleNamespace(members=members_intent),
        guilds=[guild],
        user=None,
        _allows_guild=MagicMock(return_value=allowed),
        _audience_sync=_Recorder(),
        _member_sync=_Recorder(),
        _audience_renewal=None,
        membership_resolver=MagicMock(),
        runtime=types.SimpleNamespace(sessionmaker=None),
        is_ready=lambda: ready,
        _spawn=asyncio.create_task,
        get_guild=MagicMock(return_value=guild),
        guild=guild,
    )
    bot._start_audience_renewal = types.MethodType(DaimonBot._start_audience_renewal, bot)
    return bot


@pytest.fixture(autouse=True)
def quiet_discord_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("daimon.adapters.discord.bot.remember_guild_channels", MagicMock())
    monkeypatch.setattr("daimon.adapters.discord.bot.clear_webhook_backoff", MagicMock())
    monkeypatch.setattr("daimon.adapters.discord.bot.member_facts", MagicMock())
    monkeypatch.setattr("daimon.adapters.discord.bot.update_lifecycle", AsyncMock())


async def test_a_disconnect_stops_the_renewal_and_marks_every_server_unknown() -> None:
    DaimonBot = _bot_class()

    bot = _bot()
    bot._audience_renewal = asyncio.create_task(asyncio.sleep(3600))
    renewal = bot._audience_renewal

    await DaimonBot.on_disconnect(bot)
    await asyncio.sleep(0)

    assert renewal.cancelled()
    assert bot._audience_sync.calls == [("disconnected", ([bot.guild],))]


async def test_a_resume_restarts_the_renewal_first_and_reconciles_with_the_members_intent() -> None:
    DaimonBot = _bot_class()

    bot = _bot()
    renewed = asyncio.Event()

    async def run(_guilds: object) -> None:
        renewed.set()

    async def stuck(_guilds: object) -> None:
        await asyncio.Event().wait()

    bot._audience_sync.run = run
    bot._audience_sync.resumed = stuck
    bot._audience_renewal = stopped = asyncio.create_task(asyncio.sleep(0))
    await stopped
    resuming = asyncio.create_task(DaimonBot.on_resumed(bot))
    await asyncio.wait_for(renewed.wait(), timeout=1)
    assert bot._audience_renewal is not stopped, "renewal runs even while the resume waits"
    resuming.cancel()

    bot = _bot()
    bot._audience_sync.run = run
    await DaimonBot.on_resumed(bot)
    assert bot._audience_sync.calls == [("resumed", ([bot.guild],))]
    no_intent = _bot(members_intent=False)
    no_intent._audience_sync.run = run
    await DaimonBot.on_resumed(no_intent)
    assert no_intent._audience_sync.calls == [], "no members intent, no facts"


async def test_a_returning_server_reloads_and_an_unavailable_one_becomes_unknown() -> None:
    DaimonBot = _bot_class()

    bot = _bot()
    await DaimonBot.on_guild_available(bot, bot.guild)
    await asyncio.sleep(0)
    assert bot._member_sync.calls == [("reload_all", ([bot.guild],))]

    for each in (_bot(ready=False), _bot(allowed=False), _bot(members_intent=False)):
        await DaimonBot.on_guild_available(each, each.guild)
        await asyncio.sleep(0)
        assert each._member_sync.calls == [], "startup reload or no members intent covers it"

    await DaimonBot.on_guild_unavailable(bot, bot.guild)
    assert bot._audience_sync.calls == [("mark_unknown", (bot.guild,))]
    bot.membership_resolver.invalidate_guild.assert_called_once_with("1")


async def test_audience_events_withdraw_the_facts_and_thread_events_only_refresh() -> None:
    DaimonBot = _bot_class()

    def roles(count: int) -> list[object]:
        return [object() for _ in range(count)]

    def member(bot: types.SimpleNamespace, role_list: list[object]) -> Any:
        return types.SimpleNamespace(guild=bot.guild, id=5, roles=role_list)

    def channel(bot: types.SimpleNamespace, **kw: Any) -> Any:
        base: dict[str, Any] = {"guild": bot.guild, "overwrites": {}, "name": "a", "type": 0}
        return types.SimpleNamespace(**{**base, **kw})

    def role(bot: types.SimpleNamespace, permissions: int = 0) -> Any:
        return types.SimpleNamespace(guild=bot.guild, members=[], permissions=permissions)

    bot = _bot()
    bot.runtime = types.SimpleNamespace(sessionmaker=MagicMock(return_value=AsyncMock()))
    r1 = roles(1)
    new_owner = types.SimpleNamespace(**{**vars(bot.guild), "owner_id": 8})
    thread_update = types.SimpleNamespace(
        guild_id=1, parent_id=2, thread_id=3, data={"thread_metadata": {"archived": False}}
    )
    cases: list[tuple[str, str, Callable[[], Any]]] = [
        ("role create", "schedule", lambda: DaimonBot.on_guild_role_create(bot, role(bot))),
        (
            "role update",
            "schedule",
            lambda: DaimonBot.on_guild_role_update(bot, role(bot, 0), role(bot, 8)),
        ),
        ("role delete", "schedule", lambda: DaimonBot.on_guild_role_delete(bot, role(bot))),
        ("member join", "schedule", lambda: DaimonBot.on_member_join(bot, member(bot, r1))),
        (
            "member roles",
            "schedule",
            lambda: DaimonBot.on_member_update(bot, member(bot, r1), member(bot, roles(2))),
        ),
        (
            "member leaves",
            "schedule",
            lambda: DaimonBot.on_raw_member_remove(
                bot, types.SimpleNamespace(guild_id=1, user=types.SimpleNamespace(id=5))
            ),
        ),
        (
            "channel create",
            "schedule",
            lambda: DaimonBot.on_guild_channel_create(bot, channel(bot)),
        ),
        (
            "channel delete",
            "schedule",
            lambda: DaimonBot.on_guild_channel_delete(bot, channel(bot)),
        ),
        (
            "channel overwrites",
            "schedule",
            lambda: DaimonBot.on_guild_channel_update(
                bot, channel(bot), channel(bot, overwrites={1: 1})
            ),
        ),
        (
            "channel renamed",
            "schedule",
            lambda: DaimonBot.on_guild_channel_update(bot, channel(bot), channel(bot, name="b")),
        ),
        ("owner change", "schedule", lambda: DaimonBot.on_guild_update(bot, bot.guild, new_owner)),
        (
            "thread create",
            "refresh",
            lambda: DaimonBot.on_thread_create(bot, types.SimpleNamespace(guild=bot.guild)),
        ),
        ("thread update", "refresh", lambda: DaimonBot.on_raw_thread_update(bot, thread_update)),
    ]
    for label, call, event in cases:
        before = len(bot._audience_sync.calls)
        await event()
        expected = bot.guild if label != "owner change" else new_owner
        assert bot._audience_sync.calls[before:] == [(call, (expected,))], label

    # An event that changes no one's access schedules nothing.
    for label, quiet in [
        (
            "same roles",
            lambda: DaimonBot.on_member_update(bot, member(bot, r1), member(bot, r1)),
        ),
        (
            "same channel",
            lambda: DaimonBot.on_guild_channel_update(bot, channel(bot), channel(bot)),
        ),
        ("same permissions", lambda: DaimonBot.on_guild_role_update(bot, role(bot), role(bot))),
        ("same owner", lambda: DaimonBot.on_guild_update(bot, bot.guild, bot.guild)),
    ]:
        before = len(bot._audience_sync.calls)
        await quiet()
        assert bot._audience_sync.calls[before:] == [], label

    # Role and member events also reload the admin flags in the member list.
    assert {"admins_changed", "joined", "left"} <= set(bot._member_sync.names())


async def test_events_of_a_server_the_bot_does_not_serve_are_ignored() -> None:
    DaimonBot = _bot_class()

    bot = _bot(allowed=False)
    guild = bot.guild
    await DaimonBot.on_guild_channel_create(bot, types.SimpleNamespace(guild=guild))
    await DaimonBot.on_thread_create(bot, types.SimpleNamespace(guild=guild))
    await DaimonBot.on_guild_role_create(bot, types.SimpleNamespace(guild=guild))
    await DaimonBot.on_guild_unavailable(bot, guild)
    assert bot._audience_sync.calls == []


async def test_a_channel_event_on_the_bot_withdraws_and_rereads_the_real_facts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A real AudienceSync behind the handlers: an event withdraws a room's facts until a
    read publishes the change, and a disconnect withdraws them again."""
    DaimonBot = _bot_class()

    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()
    bot = _bot()
    bot._audience_sync = audience
    bot.guilds = [world.guild]
    _open_room_one(world.server)

    await DaimonBot.on_guild_channel_create(bot, types.SimpleNamespace(guild=world.guild))
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained

    bot._audience_renewal = None
    await DaimonBot.on_disconnect(bot)
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS


async def test_renewal_keeps_ticking_after_a_tick_fails() -> None:
    audience = AudienceSync(MagicMock(), renew_every_s=0)
    ticks: list[int] = []

    async def tick(_guilds: object) -> None:
        ticks.append(len(ticks))
        if len(ticks) == 1:
            raise AttributeError("a broken read")
        if len(ticks) == 3:
            raise asyncio.CancelledError

    audience.tick = tick  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await audience.run(lambda: [])

    assert ticks == [0, 1, 2], "the tick after a failure still runs; cancel still ends it"
