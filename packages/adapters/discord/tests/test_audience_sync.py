"""Room facts follow the gateway: published after a full load, unknown whenever the cache
cannot be trusted. Nothing here waits real time."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import discord
from daimon.adapters.discord.audience_sync import AudienceSync
from daimon.adapters.discord.member_sync import MemberSync, tenant_of
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.stores import audience_facts as store
from daimon.core.stores.access_policy import set_access_policy
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    *,
    internal: tuple[str, ...] = ROLES,
    team: tuple[str, ...] = (str(fake.CORE),),
) -> None:
    await make_tenant(db_session, platform="discord", workspace_id=str(fake.GUILD))
    await set_access_policy(
        db_session,
        tenant_id=TENANT,
        policy=TenantAccessPolicy(internal_role_ids=internal, team_role_ids=team),
    )
    await db_session.commit()


async def _fact(
    factory: async_sessionmaker[AsyncSession], clock: Clock, channel_id: str
) -> RoomFacts:
    async with factory() as session:
        return await store.read_channel_facts(
            session, tenant_id=TENANT, channel_id=channel_id, now=clock.now
        )


def _server(*, chunked: bool = True) -> object:
    guest = fake.internal_only()
    guest[fake.target(discord.User, 30)] = discord.PermissionOverwrite(view_channel=True)
    return fake.guild(
        channels=[
            fake.channel(1, "planning", fake.internal_only()),
            fake.channel(2, "guest", guest),
        ],
        threads=[fake.thread(3, 1, private=True)],
        chunked=chunked,
    )


def _sync(
    factory: async_sessionmaker[AsyncSession], clock: Clock
) -> tuple[AudienceSync, MemberSync]:
    audience = AudienceSync(factory, clock=clock, fresh_for_s=180, debounce_s=0)
    return audience, MemberSync(factory, clock=clock, audience=audience)


async def test_a_full_reload_publishes_and_every_unknown_signal_withdraws(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, members = _sync(db_session_factory, clock)
    server = _server(chunked=False)
    server.chunk = AsyncMock(side_effect=lambda **_: setattr(server, "chunked", True))  # type: ignore[attr-defined]

    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "nothing before a load"
    await members.reload(server)  # type: ignore[arg-type]
    assert await _fact(db_session_factory, clock, "1") == RoomFacts(False, False)
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False)
    assert await _fact(db_session_factory, clock, "3") == RoomFacts(False, True), "private thread"

    await audience.disconnected([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "gateway lost"
    await audience.resumed([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "recomputed"


async def test_a_partial_chunk_and_a_chunk_failure_leave_every_room_unknown(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, members = _sync(db_session_factory, clock)
    server = _server(chunked=True)
    await audience.publish(server)  # type: ignore[arg-type]

    server.chunked = False  # type: ignore[attr-defined]
    await audience.publish(server)  # type: ignore[arg-type]
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "partial member cache"

    server.chunked = True  # type: ignore[attr-defined]
    await audience.publish(server)  # type: ignore[arg-type]
    server.chunked = False  # type: ignore[attr-defined]
    server.chunk = AsyncMock(side_effect=TimeoutError)  # type: ignore[attr-defined]
    await members.reload_all([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "chunk failure"


async def test_the_reload_start_makes_rooms_unknown_until_it_ends(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, members = _sync(db_session_factory, clock)
    server = _server()
    await audience.publish(server)  # type: ignore[arg-type]
    seen: list[RoomFacts] = []

    async def while_chunking(**_: object) -> None:
        seen.append(await _fact(db_session_factory, clock, "1"))
        audience.schedule(server)  # type: ignore[arg-type]
        await audience.settle()

    server.chunked = False  # type: ignore[attr-defined]
    server.chunk = AsyncMock(side_effect=while_chunking)  # type: ignore[attr-defined]
    await members.reload(server)  # type: ignore[arg-type]

    assert seen == [UNKNOWN_FACTS], "unknown during the reload"
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, (
        "an event during the reload published nothing; the cache was still filling"
    )


async def test_events_recompute_after_a_wait_in_one_write(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    await audience.publish(server)  # type: ignore[arg-type]
    assert not (await _fact(db_session_factory, clock, "1")).contained

    server.channels[0].overwrites[fake.target(discord.User, 30)] = discord.PermissionOverwrite(  # type: ignore[attr-defined]
        view_channel=True
    )
    for _ in range(5):
        audience.schedule(server)  # type: ignore[arg-type]
    assert len(audience._pending) == 1, "five events share one run"  # type: ignore[reportPrivateUsage]
    await audience.settle()

    assert (await _fact(db_session_factory, clock, "1")).contained
    async with db_session_factory() as session:
        status = await store.read_status(session, tenant_id=TENANT, now=clock.now)
    assert status is not None and status.generation == 2, "one new generation for the burst"


async def test_the_gateway_owner_renews_and_a_stalled_one_lapses(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    await audience.publish(server)  # type: ignore[arg-type]

    for _ in range(4):
        clock.now += timedelta(seconds=60)
        await audience.renew([server])  # type: ignore[list-item]
    assert (await _fact(db_session_factory, clock, "2")).contained, "renewed past 180 s"
    assert clock.now - START == timedelta(seconds=240)

    clock.now += timedelta(seconds=181)  # no renewal: the process stalled or died
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS


async def test_a_policy_change_in_another_process_is_picked_up_at_renewal(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    await audience.publish(server)  # type: ignore[arg-type]
    assert (await _fact(db_session_factory, clock, "2")).contained

    # The friend role stops being internal: the friend now counts as an outsider.
    await set_access_policy(
        db_session,
        tenant_id=TENANT,
        policy=TenantAccessPolicy(internal_role_ids=(str(fake.CORE),)),
    )
    await db_session.commit()
    clock.now += timedelta(seconds=60)
    await audience.renew([server])  # type: ignore[list-item]

    assert (await _fact(db_session_factory, clock, "1")).contained, "the friend is an outsider"


async def test_a_server_without_internal_roles_gets_no_facts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory, internal=(), team=())
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    await audience.publish(_server())  # type: ignore[arg-type]

    async with db_session_factory() as session:
        assert await store.read_status(session, tenant_id=TENANT, now=clock.now) is None


async def test_losing_the_members_intent_marks_every_server_unknown(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    await audience.publish(_server())  # type: ignore[arg-type]

    async with db_session_factory.begin() as session:
        await store.mark_all_unknown(session, now=clock.now)

    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS


def test_the_unknown_signals_are_wired_in_the_bot() -> None:
    from daimon.adapters.discord.bot import DaimonBot

    handlers: list[Callable[..., object]] = [
        DaimonBot.on_disconnect,
        DaimonBot.on_resumed,
        DaimonBot.on_guild_role_create,
        DaimonBot.on_guild_channel_create,
        DaimonBot.on_guild_channel_delete,
        DaimonBot.on_thread_create,
    ]
    assert all(inspect.iscoroutinefunction(handler) for handler in handlers)


async def test_a_resume_starts_the_renewal_again_and_a_returning_server_reloads() -> None:
    """on_disconnect stops the renewal and a resume fires no on_ready: without a restart
    here the facts lapse and every room stays unknown until the next full connect."""
    import types

    from daimon.adapters.discord.bot import DaimonBot

    renewed = asyncio.Event()
    reloaded: list[object] = []

    class _Audience:
        async def resumed(self, guilds: object) -> None:
            return None

        async def run(self, guilds: object) -> None:
            renewed.set()

    class _Members:
        async def reload_all(self, guilds: list[object]) -> None:
            reloaded.extend(guilds)

    guild = types.SimpleNamespace(id=1)
    stopped: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))
    await stopped
    bot = types.SimpleNamespace(
        intents=types.SimpleNamespace(members=True),
        guilds=[guild],
        _allows_guild=lambda _id: True,
        _audience_sync=_Audience(),
        _audience_renewal=stopped,
        _member_sync=_Members(),
        is_ready=lambda: True,
        _spawn=asyncio.create_task,
    )
    bot._start_audience_renewal = types.MethodType(DaimonBot._start_audience_renewal, bot)

    await DaimonBot.on_resumed(bot)  # type: ignore[arg-type]
    await asyncio.wait_for(renewed.wait(), timeout=1)
    assert bot._audience_renewal is not stopped

    await DaimonBot.on_guild_available(bot, guild)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert reloaded == [guild]
