"""Room facts come from one REST reconcile per server and fail closed: unknown whenever a
change, an outage or a stuck read means they cannot be vouched for. Discord is a fake
(`audience_fakes.RestDiscord`); the clock is simulated and real waits are milliseconds."""

from __future__ import annotations

import asyncio
import types
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from daimon.adapters.discord import audience_sync
from daimon.adapters.discord.audience_sync import AudienceSync
from daimon.adapters.discord.member_sync import tenant_of
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.stores import audience_facts as store
from daimon.core.stores import room_viewers
from daimon.core.stores.access_policy import AccessPolicyUnreadable, set_access_policy
from daimon.testing.factories import make_tenant
from sqlalchemy import event as sa_event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
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


@pytest.mark.parametrize(
    ("where", "name"),
    [
        (audience_sync, "get_tenant"),
        (audience_sync, "load_access_policy"),
        (audience_sync, "lock_policy_writes_shared"),
        (audience_sync, "_policy_roles"),
        (store, "publish_facts"),
    ],
)
async def test_a_change_during_any_await_of_the_publish_rolls_it_back(
    pooled: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    where: object,
    name: str,
) -> None:
    """An event that lands while the facts are being written stops them before the
    commit: right after the publishing transaction, no reader sees the crossed read."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    await audience.tick([world.guild])  # the read is asked for; the policy was read
    real = getattr(where, name)
    armed = [True]
    seen: list[RoomFacts] = []

    async def event_then_real(*args: Any, **kwargs: Any) -> Any:
        if armed[0]:
            armed[0] = False
            audience.schedule(world.guild)
        return await real(*args, **kwargs)

    real_retire = store.retire_facts

    async def look_then_retire(*args: Any, **kwargs: Any) -> None:
        seen.append(await _fact(pooled, clock, "1"))  # what a reader sees between the two
        await real_retire(*args, **kwargs)

    monkeypatch.setattr(where, name, event_then_real)
    monkeypatch.setattr(store, "retire_facts", look_then_retire)
    with capture_logs() as logs:
        await audience.settle()

    assert not armed[0], "the event fired inside the publish"
    assert seen and all(f == UNKNOWN_FACTS for f in seen), "the crossed write never showed"
    assert "discord.audience.read_crossed" in [e["event"] for e in logs]
    assert await _fact(pooled, clock, "1") == RoomFacts(False, False), "the next read lands"


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
    # The wait is real time and bounds every database step, healthy ones too: 0.05 s
    # let a slow runner time out server one's publish, which then read a third time.
    # Only server two's step is meant to run into it, once per tick.
    audience = world.sync(factory, clock, db_wait_s=0.25)
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
        assert loop.time() - started < 2, "a tick never waits for a read"
        await asyncio.sleep(0.005)
    assert (await _fact(factory, clock, "2")).contained, "server one renewed throughout"
    assert world.servers[0].reads["guild"] == 2, "and got its 300 s reconcile"
    assert await _fact(factory, clock, "2", guild_id=4001) == UNKNOWN_FACTS, "two lapsed"

    started = loop.time()
    await audience.disconnected(world.guilds)  # cancels our own stuck read
    assert loop.time() - started < 2
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


async def test_one_unreadable_policy_holds_back_no_other_servers_reconcile(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _setup(db_session)
    await _setup(db_session, guild_id=4001)
    clock, world = Clock(), World(fake.GUILD, 4001)
    audience = world.sync(db_session_factory, clock)
    real = audience_sync._policy_roles  # pyright: ignore[reportPrivateUsage]

    async def broken_for_one(session: AsyncSession, tenant_id: Any) -> Any:
        if tenant_id == TENANT:
            raise AccessPolicyUnreadable(tenant_id=tenant_id)
        return await real(session, tenant_id)

    monkeypatch.setattr(audience_sync, "_policy_roles", broken_for_one)
    with capture_logs() as logs:
        await audience.tick(world.guilds)
        await audience.settle()

    assert (await _fact(db_session_factory, clock, "2", guild_id=4001)).contained
    assert await _status(db_session_factory, clock) is None, "the unreadable one: nothing"
    failed = [e for e in logs if e["event"] == "discord.audience.policy_read_failed"]
    assert [e["tenant_id"] for e in failed] == [str(TENANT)]


def _gateway(guild_id: int = fake.GUILD) -> Any:
    """discord.py's own connection state and parsers, with one cached server and no
    cached members."""
    client = discord.Client(intents=discord.Intents(guilds=True, members=True))
    state = client._connection  # pyright: ignore[reportPrivateUsage]
    guild = discord.Guild(
        state=state,
        data=cast(
            "Any",
            {"id": str(guild_id), "name": "Sim", "owner_id": str(fake.OWNER), "roles": []},
        ),
    )
    state._add_guild(guild)  # pyright: ignore[reportPrivateUsage]
    return state


def _raw_member(user_id: int, *roles: int) -> dict[str, Any]:
    return {
        "guild_id": str(fake.GUILD),
        "user": {"id": str(user_id), "username": "someone", "discriminator": "0", "avatar": None},
        "roles": [str(r) for r in roles],
        "joined_at": "2026-04-01T00:00:00+00:00",
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


def _raw_guild(owner_id: int) -> dict[str, Any]:
    return {"id": str(fake.GUILD), "name": "Sim", "owner_id": str(owner_id), "roles": []}


def _raw_role(role_id: int, permissions: int) -> dict[str, Any]:
    role = {"id": str(role_id), "name": "r", "permissions": str(permissions), "position": 1}
    return {"guild_id": str(fake.GUILD), "role": {**role, "color": 0, "hoist": False}}


def _raw_channel(server: fake.RestServer, index: int, **changes: Any) -> dict[str, Any]:
    return {**server.channels[index], "guild_id": str(fake.GUILD), "position": 0, **changes}


async def test_raw_gateway_events_withdraw_the_facts_even_for_a_member_not_in_the_cache(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    """discord.py's real parsers, watched before they run. A member that is not cached
    dispatches no member update, yet gaining a role withdraws the facts. An event that
    leaves every input of the last read as it was withdraws nothing."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _gateway()
    audience_sync.watch_gateway(
        state.parsers, lambda e, g, d: audience.gateway_event(e, g, d, world.guild)
    )
    await audience.tick([world.guild])
    await audience.settle()
    reads = world.server.reads["guild"]

    unchanged: list[tuple[str, dict[str, Any]]] = [
        ("GUILD_MEMBER_UPDATE", {**_raw_member(fake.OUTSIDER), "nick": "a new nickname"}),
        ("GUILD_ROLE_UPDATE", _raw_role(fake.CORE, fake.VIEW_BIT | fake.READ_BIT)),
        ("CHANNEL_UPDATE", _raw_channel(world.server, 0, topic="a new topic")),
        ("GUILD_UPDATE", _raw_guild(fake.OWNER)),
        ("CHANNEL_CREATE", {"id": "77", "type": 1, "recipients": []}),  # a DM: no room's
    ]
    for event, data in unchanged:
        state.parsers[event](data)
        await asyncio.sleep(0.005)
        assert (await _fact(pooled, clock, "1")).known, event
    assert world.server.reads["guild"] == reads, "nothing moved, nothing read"

    changed: list[tuple[str, dict[str, Any]]] = [
        ("GUILD_MEMBER_UPDATE", _raw_member(fake.OUTSIDER, fake.FRIEND)),  # not cached
        ("GUILD_MEMBER_ADD", _raw_member(50)),
        ("GUILD_MEMBER_REMOVE", {"guild_id": str(fake.GUILD), "user": _raw_member(20)["user"]}),
        ("GUILD_ROLE_UPDATE", _raw_role(fake.FRIEND, fake.VIEW_BIT)),
        ("GUILD_ROLE_CREATE", _raw_role(60, 0)),
        ("GUILD_ROLE_DELETE", {"guild_id": str(fake.GUILD), "role_id": "60"}),
        (
            "CHANNEL_UPDATE",
            _raw_channel(world.server, 0, permission_overwrites=[]),
        ),
        ("CHANNEL_CREATE", _raw_channel(world.server, 1, id="8", name="new")),
        ("CHANNEL_DELETE", _raw_channel(world.server, 1, id="8", name="new")),
        ("GUILD_UPDATE", _raw_guild(20)),
    ]

    async def withdrawn() -> bool:
        return await _fact(pooled, clock, "1") == UNKNOWN_FACTS

    for event, data in changed:
        gate = _gate(world.server)
        state.parsers[event](data)
        await _eventually(withdrawn)
        gate.set()
        await audience.settle()
        assert (await _fact(pooled, clock, "1")).known, f"{event}: read again"
    assert state._get_guild(fake.GUILD).get_member(fake.OUTSIDER) is not None, (  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
        "the parser still ran"
    )

    def broken(_event: str, _guild_id: int, _data: object) -> None:
        raise RuntimeError("a broken watcher")

    other = _gateway()
    audience_sync.watch_gateway(other.parsers, broken)
    other.parsers["GUILD_MEMBER_UPDATE"](_raw_member(fake.OUTSIDER, fake.FRIEND))
    assert other._get_guild(fake.GUILD).get_member(fake.OUTSIDER) is not None, (  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
        "a failing watcher never stops discord.py"
    )


# --- the bot's handlers -----------------------------------------------------------------


class _Recorder:
    """Stands in for AudienceSync or MemberSync on a bot stub and records each call."""

    _SYNC = frozenset({"schedule", "refresh", "request_reconcile", "gateway_event", "connected"})

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
    bot._audience_event = types.MethodType(DaimonBot._audience_event, bot)
    return bot


@pytest.fixture(autouse=True)
def quiet_discord_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("daimon.adapters.discord.bot.remember_guild_channels", MagicMock())
    monkeypatch.setattr("daimon.adapters.discord.bot.clear_webhook_backoff", MagicMock())
    monkeypatch.setattr("daimon.adapters.discord.bot.member_facts", MagicMock())
    monkeypatch.setattr("daimon.adapters.discord.bot.update_lifecycle", AsyncMock())


async def test_a_disconnect_keeps_the_renewal_loop_and_marks_every_server_unknown() -> None:
    DaimonBot = _bot_class()

    bot = _bot()
    bot._audience_renewal = asyncio.create_task(asyncio.sleep(3600))
    renewal = bot._audience_renewal

    await DaimonBot.on_disconnect(bot)
    await asyncio.sleep(0)

    assert not renewal.done(), "the loop lives for the process"
    assert bot._audience_sync.calls == [("disconnected", ([bot.guild],))]
    renewal.cancel()


async def test_exactly_one_renewal_loop_runs_after_any_disconnect_and_resume_order() -> None:
    """A loop that was cancelled and is still cleaning up used to hide from the resume,
    which then started none. No disconnect cancels it now, so one loop always runs."""
    DaimonBot = _bot_class()

    bot = _bot()
    release = asyncio.Event()
    starts: list[int] = []

    async def run(_guilds: object) -> None:
        starts.append(1)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()  # cleanup of a database step that has not ended yet
            raise

    bot._audience_sync.run = run
    bot._start_audience_renewal()
    await asyncio.sleep(0)
    for _ in range(2):
        await DaimonBot.on_disconnect(bot)
        await DaimonBot.on_resumed(bot)
    await DaimonBot.on_disconnect(bot)
    await asyncio.sleep(0)
    release.set()
    await asyncio.sleep(0.01)
    await DaimonBot.on_resumed(bot)
    await asyncio.sleep(0)

    loops = [
        t
        for t in asyncio.all_tasks()
        if t.get_name() == "discord.audience_renewal" and not t.done()
    ]
    assert len(loops) == 1 and loops[0] is bot._audience_renewal
    assert starts == [1]
    loops[0].cancel()
    release.set()


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
    assert bot._audience_sync.calls == [("connected", ()), ("resumed", ([bot.guild],))]
    no_intent = _bot(members_intent=False)
    no_intent._audience_sync.run = run
    await DaimonBot.on_resumed(no_intent)
    assert no_intent._audience_sync.calls == [("connected", ())], "no members intent, no read"


async def test_a_ready_marks_the_gateway_connected_and_renews_before_any_slower_step() -> None:
    DaimonBot = _bot_class()
    bot = _bot()
    bot.runtime = types.SimpleNamespace(
        settings=types.SimpleNamespace(
            environment=types.SimpleNamespace(name="test"),
            discord=types.SimpleNamespace(command_guild_ids=[]),
        ),
        sessionmaker=None,
    )
    bot._non_production = False
    renewing = asyncio.Event()

    async def run(_guilds: object) -> None:
        renewing.set()

    class Reached(Exception):
        pass

    async def retire_orphaned_turns() -> None:
        await asyncio.sleep(0)  # the renewal task gets its first turn
        assert renewing.is_set(), "renewal runs before the first slower step of the ready"
        raise Reached

    bot._audience_sync.run = run
    bot._retire_orphaned_turns = retire_orphaned_turns
    with pytest.raises(Reached):
        await DaimonBot.on_ready(bot)
    assert bot._audience_sync.calls == [("connected", ())]


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


async def test_handlers_leave_withdrawal_to_the_raw_events_and_threads_only_refresh() -> None:
    """Role, member, channel and owner handlers still keep the member list, but never
    touch the room facts: the raw gateway event did that before they ran. Thread events
    only ask for a read."""
    DaimonBot = _bot_class()

    def member(bot: types.SimpleNamespace, role_count: int) -> Any:
        return types.SimpleNamespace(guild=bot.guild, id=5, roles=[object()] * role_count)

    def channel(bot: types.SimpleNamespace, **kw: Any) -> Any:
        base: dict[str, Any] = {"guild": bot.guild, "overwrites": {}, "name": "a", "type": 0}
        return types.SimpleNamespace(**{**base, **kw})

    def role(bot: types.SimpleNamespace, permissions: int = 0) -> Any:
        return types.SimpleNamespace(guild=bot.guild, members=[], permissions=permissions)

    bot = _bot()
    bot.runtime = types.SimpleNamespace(sessionmaker=MagicMock(return_value=AsyncMock()))
    new_owner = types.SimpleNamespace(**{**vars(bot.guild), "owner_id": 8})
    leaves = types.SimpleNamespace(guild_id=1, user=types.SimpleNamespace(id=5))
    for handler in [
        DaimonBot.on_guild_role_update(bot, role(bot, 0), role(bot, 8)),
        DaimonBot.on_guild_role_delete(bot, role(bot)),
        DaimonBot.on_member_join(bot, member(bot, 1)),
        DaimonBot.on_member_update(bot, member(bot, 1), member(bot, 2)),
        DaimonBot.on_raw_member_remove(bot, leaves),
        DaimonBot.on_guild_channel_update(bot, channel(bot), channel(bot, overwrites={1: 1})),
        DaimonBot.on_guild_update(bot, bot.guild, new_owner),
    ]:
        await handler
    assert bot._audience_sync.calls == []
    assert {"admins_changed", "joined", "left"} <= set(bot._member_sync.names())

    thread_update = types.SimpleNamespace(
        guild_id=1, parent_id=2, thread_id=3, data={"thread_metadata": {"archived": False}}
    )
    await DaimonBot.on_thread_create(bot, types.SimpleNamespace(guild=bot.guild))
    await DaimonBot.on_raw_thread_update(bot, thread_update)
    assert bot._audience_sync.calls == [("refresh", (bot.guild,)), ("refresh", (bot.guild,))]

    data = {"guild_id": "1"}
    bot._audience_event("GUILD_ROLE_CREATE", 1, data)
    assert bot._audience_sync.calls[-1] == (
        "gateway_event",
        ("GUILD_ROLE_CREATE", 1, data, bot.guild),
    )


async def test_a_built_bot_watches_the_raw_gateway_events() -> None:
    from daimon.testing.discord_builders import discord_runtime
    from daimon.testing.settings_mock import mock_settings

    runtime = discord_runtime(
        settings=mock_settings(), anthropic=AsyncMock(), sessionmaker=MagicMock()
    )
    bot = _bot_class()(runtime=runtime, intents=discord.Intents.default())
    bot._audience_sync = recorder = _Recorder()
    bot._allows_guild = MagicMock(return_value=True)
    data = {"guild_id": str(fake.GUILD), "role_id": "60"}
    bot._connection.parsers["GUILD_ROLE_DELETE"](data)  # pyright: ignore[reportPrivateUsage]
    assert recorder.calls == [("gateway_event", ("GUILD_ROLE_DELETE", fake.GUILD, data, None))]


async def test_events_of_a_server_the_bot_does_not_serve_are_ignored() -> None:
    DaimonBot = _bot_class()

    bot = _bot(allowed=False)
    guild = bot.guild
    bot._audience_event("CHANNEL_CREATE", 1, {"guild_id": "1"})
    await DaimonBot.on_thread_create(bot, types.SimpleNamespace(guild=guild))
    await DaimonBot.on_guild_unavailable(bot, guild)
    assert bot._audience_sync.calls == []


async def test_a_raw_event_on_the_bot_withdraws_and_rereads_the_real_facts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A real AudienceSync behind the bot: an event withdraws a room's facts until a read
    publishes the change, and a disconnect withdraws them again."""
    DaimonBot = _bot_class()

    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()
    bot = _bot()
    bot._audience_sync = audience
    bot.guilds = [world.guild]
    bot.get_guild = MagicMock(return_value=world.guild)
    _open_room_one(world.server)

    bot._audience_event("CHANNEL_UPDATE", fake.GUILD, _raw_channel(world.server, 0))
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


async def test_nickname_events_during_every_read_after_a_real_change_never_starve_the_server(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    """After a real role change the published facts are gone, but the last read's inputs
    stay as the comparison: an event that states what they say crosses no read. Each
    read below gets one nickname-only member event and each publishes."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _gateway()
    audience_sync.watch_gateway(
        state.parsers, lambda e, g, d: audience.gateway_event(e, g, d, world.guild)
    )
    await audience.tick([world.guild])
    await audience.settle()
    assert (await _fact(pooled, clock, "1")).known

    gate = _gate(world.server)
    world.server.roles[fake.FRIEND] = fake.VIEW_BIT  # a real change, as Discord now has it
    state.parsers["GUILD_ROLE_UPDATE"](_raw_role(fake.FRIEND, fake.VIEW_BIT))
    for n in range(4):
        reading = asyncio.create_task(audience.reconcile(world.guild)) if n else None
        await _eventually(lambda n=n: _members_read(world, n + 2))
        nick = {**_raw_member(fake.OUTSIDER), "nick": f"nickname {n}"}
        state.parsers["GUILD_MEMBER_UPDATE"](nick)
        gate.set()
        if reading is None:
            await audience.settle()
        else:
            assert await reading, f"read {n} published"
        assert (await _fact(pooled, clock, "1")).known, f"read {n} published"
        gate = _gate(world.server)
    assert world.server.reads["members"] == 5, "no read was crossed and read again"


async def test_a_real_change_during_a_read_prevents_its_publication(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    """An event that differs from the basis and from what the read saw crosses the read."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    await audience.tick([world.guild])
    await audience.settle()
    state = _gateway()
    audience_sync.watch_gateway(
        state.parsers, lambda e, g, d: audience.gateway_event(e, g, d, world.guild)
    )

    gate = _gate(world.server)
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])
    await _eventually(lambda: _members_read(world, 2))
    state.parsers["GUILD_MEMBER_UPDATE"](_raw_member(fake.OUTSIDER, fake.FRIEND))  # not read yet
    with capture_logs() as logs:
        gate.set()
        await audience.settle()
    assert [e["event"] for e in logs].count("discord.audience.read_crossed") == 1
    assert world.server.reads["guild"] == 3, "the crossed read, then one more"


async def test_an_event_that_states_what_the_read_saw_does_not_cross_it(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _gateway()
    audience_sync.watch_gateway(
        state.parsers, lambda e, g, d: audience.gateway_event(e, g, d, world.guild)
    )
    await audience.tick([world.guild])
    await audience.settle()

    world.server.roles[fake.FRIEND] = fake.VIEW_BIT
    gate = _gate(world.server)
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])
    await _eventually(lambda: _members_read(world, 2))
    # The read already has the new bits; the event for them arrives after it began.
    state.parsers["GUILD_ROLE_UPDATE"](_raw_role(fake.FRIEND, fake.VIEW_BIT))
    with capture_logs() as logs:
        gate.set()
        await audience.settle()
    names = [e["event"] for e in logs]
    assert "discord.audience.read_crossed" not in names[: names.index("discord.audience.published")]


async def test_a_change_during_the_commit_leaves_the_facts_unknown_and_nothing_renews_them(
    pooled: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last check comes before the commit. A change during it is withdrawn the moment
    the publisher releases its lock (one commit plus one withdrawal write is the window)
    and the facts are not renewed."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    await audience.tick([world.guild])
    await audience.settle()
    armed = [False]
    gates: list[asyncio.Event] = []
    real = store.publish_facts

    async def publish_then_arm(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        armed[0] = True
        return result

    def during_commit(_session: Any) -> None:
        if armed[0]:
            armed[0] = False
            monkeypatch.setattr(store, "publish_facts", real)  # only the first publish
            gates.append(_gate(world.server))  # the re-read the event asks for waits
            audience.schedule(world.guild)

    monkeypatch.setattr(store, "publish_facts", publish_then_arm)
    sa_event.listen(Session, "before_commit", during_commit)
    try:
        clock.now += timedelta(seconds=300)
        await audience.tick([world.guild])
        await _eventually(lambda: _members_read(world, 3))  # the periodic read, then the re-read

        async def withdrawn() -> bool:
            return await _fact(pooled, clock, "1") == UNKNOWN_FACTS

        await _eventually(withdrawn)
        clock.now += timedelta(seconds=60)
        await audience.tick([world.guild])
        assert await _fact(pooled, clock, "1") == UNKNOWN_FACTS, "no renewal extends them"
        assert gates, "the event came during the commit"
        gates[0].set()
        await audience.settle()
    finally:
        sa_event.remove(Session, "before_commit", during_commit)
    assert (await _fact(pooled, clock, "1")).known, "the re-read lands"


async def test_a_reconcile_asked_while_disconnected_reads_and_publishes_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session)
    clock, world = Clock(), World()
    audience = world.sync(db_session_factory, clock)
    await audience.tick([world.guild])
    await audience.settle()
    reads = dict(world.server.reads)

    await audience.disconnected([world.guild])
    audience.request_reconcile(world.guild)  # a member reload that was still running
    audience.refresh(world.guild)
    audience.schedule(world.guild)
    assert not await audience.reconcile(world.guild)
    await audience.tick([world.guild])
    await asyncio.sleep(0.01)
    await audience.settle()
    assert world.server.reads == reads, "nothing was read"
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS

    audience.connected()
    assert await audience.reconcile(world.guild)
    assert world.server.reads["guild"] == reads["guild"] + 1


def _watched(audience: AudienceSync, world: World) -> Any:
    """discord.py's parsers with the publisher watching them, as in the bot."""
    state = _gateway()
    audience_sync.watch_gateway(
        state.parsers, lambda e, g, d: audience.gateway_event(e, g, d, world.guild)
    )
    return state


def _outcomes(logs: list[Any]) -> list[str]:
    return [
        str(e["event"])
        for e in logs
        if e["event"] in ("discord.audience.published", "discord.audience.read_crossed")
    ]


async def test_an_event_during_the_commit_that_states_the_basis_withdraws_a_read_that_differs(
    pooled: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change was missed, so the read differs from the basis. An event that states the
    basis comes during the commit: it changes nothing against the basis and withdraws
    nothing by itself, so the publisher withdraws the facts and nothing renews them."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _watched(audience, world)
    await audience.tick([world.guild])
    await audience.settle()
    old = list(world.server.channels[0]["permission_overwrites"])
    world.server.channels[0]["permission_overwrites"] = [
        *old,
        fake.rest_overwrite(fake.OUTSIDER, member=True, allow=fake.VIEW_BIT),
    ]  # missed by the gateway
    armed = [False]
    fired = [False]
    real = store.publish_facts

    async def publish_then_arm(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        armed[0] = True
        return result

    def during_commit(_session: Any) -> None:
        if armed[0]:
            armed[0] = False
            fired[0] = True
            monkeypatch.setattr(store, "publish_facts", real)  # only the first publish
            state.parsers["CHANNEL_UPDATE"](
                _raw_channel(world.server, 0, permission_overwrites=old, topic="a new topic")
            )  # equals the basis

    monkeypatch.setattr(store, "publish_facts", publish_then_arm)
    sa_event.listen(Session, "before_commit", during_commit)
    try:
        clock.now += timedelta(seconds=300)
        with capture_logs() as logs:
            await audience.tick([world.guild])
            await audience.settle()
        assert fired[0], "the event came during the commit"
        assert _outcomes(logs) == ["discord.audience.read_crossed"]
        assert await _fact(pooled, clock, "1") == UNKNOWN_FACTS
        world.server.channels[0]["permission_overwrites"] = old  # the next read agrees
        gate = _gate(world.server)
        clock.now += timedelta(seconds=60)
        await audience.tick([world.guild])
        assert await _fact(pooled, clock, "1") == UNKNOWN_FACTS, "no renewal extends them"
        gate.set()
        await audience.settle()
    finally:
        sa_event.remove(Session, "before_commit", during_commit)


async def test_a_suppressed_event_still_constrains_the_read_that_is_running(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    """A channel event grants the outsider a view; the next read still gets Discord's
    older overwrites. A topic-only event with the granted overwrites then equals the
    basis and changes nothing, but it says the read is stale: that read publishes nothing."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _watched(audience, world)
    await audience.tick([world.guild])
    await audience.settle()
    old = list(world.server.channels[0]["permission_overwrites"])
    granted = [*old, fake.rest_overwrite(fake.OUTSIDER, member=True, allow=fake.VIEW_BIT)]

    gate = _gate(world.server)
    state.parsers["CHANNEL_UPDATE"](
        _raw_channel(world.server, 0, permission_overwrites=granted)
    )  # a real change; Discord's REST has not caught up
    await _eventually(lambda: _members_read(world, 2))
    state.parsers["CHANNEL_UPDATE"](
        _raw_channel(world.server, 0, permission_overwrites=granted, topic="a new topic")
    )
    with capture_logs() as logs:
        gate.set()
        await audience.settle()
    assert _outcomes(logs) == ["discord.audience.read_crossed"]
    assert await _fact(pooled, clock, "1") == UNKNOWN_FACTS, "the stale read published nothing"

    world.server.channels[0]["permission_overwrites"] = granted
    assert await audience.reconcile(world.guild)
    assert (await _fact(pooled, clock, "1")).known
    assert (await _fact(pooled, clock, "1")).contained, "the grant shows in the facts"


async def test_an_event_that_states_the_basis_does_not_vouch_for_a_read_that_differs(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    """A change was missed, so REST differs from the basis; an event then states the
    basis. The read and the event disagree, so the read publishes nothing."""
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _watched(audience, world)
    await audience.tick([world.guild])
    await audience.settle()
    old = list(world.server.channels[0]["permission_overwrites"])
    world.server.channels[0]["permission_overwrites"] = [
        *old,
        fake.rest_overwrite(fake.OUTSIDER, member=True, allow=fake.VIEW_BIT),
    ]  # missed by the gateway

    gate = _gate(world.server)
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])
    await _eventually(lambda: _members_read(world, 2))
    state.parsers["CHANNEL_UPDATE"](
        _raw_channel(world.server, 0, permission_overwrites=old, topic="a new topic")
    )  # equals the basis
    with capture_logs() as logs:
        gate.set()
        await audience.settle()
    assert _outcomes(logs) == ["discord.audience.read_crossed"]

    world.server.channels[0]["permission_overwrites"] = old
    assert await audience.reconcile(world.guild)


async def test_many_harmless_events_in_every_read_cannot_keep_a_server_without_a_basis_unknown(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _watched(audience, world)
    gate = _gate(world.server)
    await audience.tick([world.guild])  # no basis: nothing has been published
    await _eventually(lambda: _members_read(world, 1))
    for n in range(200):
        state.parsers["GUILD_MEMBER_UPDATE"]({**_raw_member(fake.OUTSIDER), "nick": f"nick {n}"})
    with capture_logs() as logs:
        gate.set()
        await audience.settle()
    assert _outcomes(logs)[0] == "discord.audience.published"
    assert "discord.audience.read_crossed" not in _outcomes(logs)
    assert (await _fact(pooled, clock, "1")).known
    assert audience._basis, "the first read set the basis"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("last_matches", "outcome"), [(True, "published"), (False, "read_crossed")]
)
async def test_only_the_last_event_per_entity_decides_whether_a_read_stands(
    pooled: async_sessionmaker[AsyncSession], last_matches: bool, outcome: str
) -> None:
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _watched(audience, world)
    await audience.tick([world.guild])
    await audience.settle()
    gained, same = _raw_member(fake.OUTSIDER, fake.FRIEND), _raw_member(fake.OUTSIDER)

    gate = _gate(world.server)
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])
    await _eventually(lambda: _members_read(world, 2))
    # The read saw the outsider with no role.
    for data in [gained, same] if last_matches else [same, gained]:
        state.parsers["GUILD_MEMBER_UPDATE"](data)
    with capture_logs() as logs:
        gate.set()
        await audience.settle()
    assert _outcomes(logs)[0] == f"discord.audience.{outcome}"


async def test_an_event_that_is_not_an_update_blocks_the_read_that_is_running(
    pooled: async_sessionmaker[AsyncSession],
) -> None:
    clock, world = Clock(), World()
    audience = world.sync(pooled, clock)
    state = _watched(audience, world)
    await audience.tick([world.guild])
    await audience.settle()

    gate = _gate(world.server)
    clock.now += timedelta(seconds=300)
    await audience.tick([world.guild])
    await _eventually(lambda: _members_read(world, 2))
    state.parsers["GUILD_MEMBER_UPDATE"](_raw_member(fake.OUTSIDER))  # equals the read
    state.parsers["GUILD_MEMBER_ADD"](_raw_member(fake.OUTSIDER + 1))
    with capture_logs() as logs:
        gate.set()
        await audience.settle()
    assert _outcomes(logs)[0] == "discord.audience.read_crossed"


@pytest.mark.parametrize(
    ("on", "reads"),
    [(True, True), (True, False), (False, True)],
    ids=["data sources and reads on", "default: no agent reads data", "data sources off"],
)
async def test_room_viewers_are_kept_as_reader_subjects_only_while_something_reads_data(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    on: bool,
    reads: bool,
) -> None:
    """Data sources are on by default, yet inert: no viewers are kept until data reads are on
    somewhere in the workspace."""
    await _setup(db_session)

    async def data_sources(*_args: object, **_kwargs: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(on=on)

    async def rows(*_args: object, **_kwargs: object) -> list[types.SimpleNamespace]:
        return [types.SimpleNamespace(value="on")] if reads else []

    monkeypatch.setattr(audience_sync, "feature_in", data_sources)
    monkeypatch.setattr(audience_sync, "list_feature_values", rows)
    clock, world = Clock(), World()
    assert await world.sync(db_session_factory, clock).reconcile(world.guild)
    async with db_session_factory() as session:
        viewers = await room_viewers.read_viewers(
            session, tenant_id=TENANT, channel_id="1", now=clock.now
        )
    if not (on and reads):
        assert viewers is None, "no viewers kept while nothing reads data"
        return
    assert viewers, "room 1 has viewers"
    assert all(v.startswith(f"discord/{fake.GUILD}:user:") for v in viewers), viewers
