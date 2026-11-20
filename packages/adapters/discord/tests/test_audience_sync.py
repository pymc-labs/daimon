"""Room facts follow the gateway: published after a full load, unknown whenever the cache
cannot be trusted. Nothing here waits real time."""

from __future__ import annotations

import asyncio
import types
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.audience_sync import AudienceSync
from daimon.adapters.discord.member_sync import MemberSync, tenant_of
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.stores import audience_facts as store
from daimon.core.stores.access_policy import set_access_policy
from daimon.testing.factories import make_tenant
from sqlalchemy.exc import SQLAlchemyError
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
    audience = AudienceSync(
        factory,
        clock=clock,
        fresh_for_s=180,
        debounce_s=0,
        reconcile_every_s=300,
        reconcile_limit_s=600,
    )
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
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "reconciled"


async def test_a_chunk_failure_leaves_every_room_unknown(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, members = _sync(db_session_factory, clock)
    server = _server(chunked=True)
    await audience.publish(server, reconciled=True)  # type: ignore[arg-type]
    assert (await _fact(db_session_factory, clock, "2")).contained

    server.chunked = False  # type: ignore[attr-defined]
    server.chunk = AsyncMock(side_effect=TimeoutError)  # type: ignore[attr-defined]
    await members.reload_all([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "chunk failure"


async def test_a_wrong_member_count_does_not_block_a_full_chunk(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """discord.py's `chunked` compares counts: after a missed join it stays False for good.
    Our own record of a complete load decides instead."""
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server(chunked=False)

    audience.schedule(server)  # type: ignore[arg-type]
    await audience.publish(server)  # type: ignore[arg-type]
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "no load yet: skipped"
    server.fetch_roles.assert_not_awaited()  # type: ignore[attr-defined]

    assert await audience.reconcile(server)  # type: ignore[arg-type]
    assert server.chunked is False  # type: ignore[attr-defined]
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False)

    server.channels[0].overwrites[fake.target(discord.User, 30)] = discord.PermissionOverwrite(  # type: ignore[attr-defined]
        view_channel=True
    )
    audience.schedule(server)  # type: ignore[arg-type]
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained, "an event publishes too"

    await audience.disconnected([server])  # type: ignore[list-item]
    await audience.publish(server)  # type: ignore[arg-type]
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "load lost at disconnect"


async def test_the_reload_start_makes_rooms_unknown_until_it_ends(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, members = _sync(db_session_factory, clock)
    server = _server()
    await audience.publish(server, reconciled=True)  # type: ignore[arg-type]
    seen: list[RoomFacts] = []

    async def while_chunking(**_: object) -> None:
        seen.append(await _fact(db_session_factory, clock, "1"))
        audience.schedule(server)  # type: ignore[arg-type]
        await audience.settle()
        await audience.publish(server)  # type: ignore[arg-type]
        seen.append(await _fact(db_session_factory, clock, "1"))

    server.chunked = False  # type: ignore[attr-defined]
    server.chunk = AsyncMock(side_effect=while_chunking)  # type: ignore[attr-defined]
    await members.reload(server)  # type: ignore[arg-type]

    assert seen == [UNKNOWN_FACTS, UNKNOWN_FACTS], (
        "unknown during the reload; an event recompute during it published nothing"
    )
    assert await _fact(db_session_factory, clock, "1") == RoomFacts(False, False), "loaded"


async def test_events_recompute_after_a_wait_in_one_write(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    await audience.publish(server, reconciled=True)  # type: ignore[arg-type]
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


async def test_renewal_is_bounded_by_the_last_reconcile_and_a_stalled_owner_lapses(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    assert await audience.reconcile(server)  # type: ignore[arg-type]

    for _ in range(4):
        clock.now += timedelta(seconds=60)
        await audience.tick([server])  # type: ignore[list-item]
    assert (await _fact(db_session_factory, clock, "2")).contained, "renewed past 180 s"
    assert server.fetch_roles.await_count == 1, "renewal asks Discord nothing"  # type: ignore[attr-defined]

    # Discord stops answering at the 300 s reconcile: renewals continue to the limit only.
    server.chunk = AsyncMock(side_effect=TimeoutError)  # type: ignore[attr-defined]
    for _ in range(5):
        clock.now += timedelta(seconds=60)
        await audience.tick([server])  # type: ignore[list-item]
    assert clock.now - START == timedelta(seconds=540)
    assert (await _fact(db_session_factory, clock, "2")).contained, "one failed reconcile is safe"

    clock.now += timedelta(seconds=60)  # 600 s after the last reconcile
    await audience.tick([server])  # type: ignore[list-item]
    clock.now += timedelta(seconds=1)
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "limit reached"

    server.chunk = AsyncMock(side_effect=lambda **_: setattr(server, "chunked", True))  # type: ignore[attr-defined]
    await audience.tick([server])  # type: ignore[list-item]
    assert (await _fact(db_session_factory, clock, "2")).contained, "the next reconcile recovers"


async def test_a_reconcile_reads_discord_and_corrects_a_missed_event(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    assert await audience.reconcile(server)  # type: ignore[arg-type]
    assert not (await _fact(db_session_factory, clock, "1")).contained

    # Discord opens the room to the outsider; the gateway event never arrived, so the
    # cache still shows it closed. Only the REST read sees the change.
    opened = fake.channel(1, "planning", fake.internal_only())
    opened.overwrites[fake.target(discord.User, 30)] = discord.PermissionOverwrite(
        view_channel=True
    )
    opened.guild = server
    server.fetch_channels = AsyncMock(return_value=[opened, server.channels[1]])  # type: ignore[attr-defined]
    clock.now += timedelta(seconds=300)
    await audience.tick([server])  # type: ignore[list-item]

    assert (await _fact(db_session_factory, clock, "1")).contained
    server.fetch_roles.assert_awaited()  # type: ignore[attr-defined]
    server.chunk.assert_awaited_with(cache=True)  # type: ignore[attr-defined]


async def test_a_reconcile_counts_the_chunked_members_so_a_missed_leave_does_not_linger(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    assert await audience.reconcile(server)  # type: ignore[arg-type]
    assert (await _fact(db_session_factory, clock, "2")).contained

    # The outsider left and the leave event was missed: the cache still holds them, the
    # fresh member list does not.
    present = [m for m in server.members if m.id != 30]  # type: ignore[attr-defined]
    server.chunk = AsyncMock(return_value=present)  # type: ignore[attr-defined]
    clock.now += timedelta(seconds=300)
    await audience.tick([server])  # type: ignore[list-item]

    assert not (await _fact(db_session_factory, clock, "2")).contained


async def test_an_event_recompute_keeps_what_a_reconcile_read_from_discord(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    assert await audience.reconcile(server)  # type: ignore[arg-type]
    assert not (await _fact(db_session_factory, clock, "1")).contained

    # A channel update was missed: the cache shows the room closed, Discord has it open.
    opened = fake.channel(1, "planning", fake.internal_only())
    opened.overwrites[fake.target(discord.User, 30)] = discord.PermissionOverwrite(
        view_channel=True
    )
    opened.guild = server
    server.fetch_channels = AsyncMock(return_value=[opened, server.channels[1]])  # type: ignore[attr-defined]
    clock.now += timedelta(seconds=300)
    await audience.tick([server])  # type: ignore[list-item]
    assert (await _fact(db_session_factory, clock, "1")).contained

    # An unrelated member event recomputes from the stale cache: no REST, no reopening.
    clock.now += timedelta(seconds=30)
    audience.schedule(server)  # type: ignore[arg-type]
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained, "the room stays contained"
    assert server.fetch_channels.await_count == 1, "only the tick reconcile asks Discord"  # type: ignore[attr-defined]


def _gated_server() -> Any:
    """A room open to a role nobody holds yet; the outsider (30) will gain that role."""
    gated = fake.internal_only()
    gated[fake.target(discord.Role, 50)] = discord.PermissionOverwrite(view_channel=True)
    return fake.guild(
        channels=[
            fake.channel(1, "planning", gated),
            fake.channel(2, "guest", fake.internal_only()),
        ]
    )


def _gained_role(server: Any) -> list[Any]:
    """The member list Discord returns once the outsider holds the role."""
    return [fake.member(30, [fake.role(50)]) if m.id == 30 else m for m in server.members]


async def test_an_event_never_reopens_what_a_reconcile_closed_after_a_missed_role_update(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _gated_server()
    assert await audience.reconcile(server)  # type: ignore[arg-type]
    assert not (await _fact(db_session_factory, clock, "1")).contained

    # The role update was missed: the cache keeps the old member, the chunk has the new one.
    server.chunk = AsyncMock(return_value=_gained_role(server))  # type: ignore[attr-defined]
    clock.now += timedelta(seconds=300)
    await audience.tick([server])  # type: ignore[list-item]
    assert (await _fact(db_session_factory, clock, "1")).contained

    audience.schedule(server)  # type: ignore[arg-type]
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained, "stays contained"
    server.fetch_roles.assert_awaited()  # type: ignore[attr-defined]
    assert server.fetch_roles.await_count == 2, "no REST read for the event"  # type: ignore[attr-defined]


async def test_a_real_opening_shows_at_the_next_reconcile_and_a_closing_at_once(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _gated_server()
    with_role = _gained_role(server)
    server.members = with_role  # type: ignore[attr-defined]
    assert await audience.reconcile(server)  # type: ignore[arg-type]
    assert (await _fact(db_session_factory, clock, "1")).contained

    # An event removes the outsider; the room stays contained until the next reconcile.
    server.members = [m for m in server.members if m.id != 30]  # type: ignore[attr-defined]
    audience.schedule(server)  # type: ignore[arg-type]
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained, "relaxed only by a reconcile"

    clock.now += timedelta(seconds=300)
    await audience.tick([server])  # type: ignore[list-item]
    assert not (await _fact(db_session_factory, clock, "1")).contained, "the reconcile opens it"

    # The other way is immediate: an event that gives someone view access shows at once.
    server.members = with_role  # type: ignore[attr-defined]
    audience.schedule(server)  # type: ignore[arg-type]
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained


async def test_a_channel_created_after_the_reconcile_uses_the_cache_result(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    assert await audience.reconcile(server)  # type: ignore[arg-type]

    everyone = {
        fake.target(discord.Role, fake.GUILD): discord.PermissionOverwrite(view_channel=True)
    }
    for channel in (
        fake.channel(4, "new-open", everyone),
        fake.channel(5, "new-closed", fake.internal_only()),
    ):
        channel.guild = server
        server.channels.append(channel)  # type: ignore[attr-defined]
    audience.schedule(server)  # type: ignore[arg-type]
    await audience.settle()

    assert (await _fact(db_session_factory, clock, "4")).contained
    assert await _fact(db_session_factory, clock, "5") == RoomFacts(False, False)
    server.channels.pop(0)  # type: ignore[attr-defined]
    audience.schedule(server)  # type: ignore[arg-type]
    await audience.settle()
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "a deleted room is dropped"


async def test_a_disconnect_clears_the_baseline(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _gated_server()
    server.chunk = AsyncMock(return_value=_gained_role(server))  # type: ignore[attr-defined]
    assert await audience.reconcile(server)  # type: ignore[arg-type]
    assert (await _fact(db_session_factory, clock, "1")).contained

    await audience.disconnected([server])  # type: ignore[list-item]
    assert not audience._baseline  # type: ignore[reportPrivateUsage]
    await audience.publish(server, reconciled=True)  # a reload from the (stale) cache
    assert not (await _fact(db_session_factory, clock, "1")).contained, "no old baseline"

    await audience.mark_unknown(server)  # type: ignore[arg-type]
    assert not audience._baseline  # type: ignore[reportPrivateUsage]


async def test_a_failed_publish_on_reconnect_recovers_at_the_next_tick(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    await audience.publish(server, reconciled=True)  # type: ignore[arg-type]
    await audience.disconnected([server])  # type: ignore[list-item]

    real = store.publish_facts
    monkeypatch.setattr(
        "daimon.adapters.discord.audience_sync.facts_store.publish_facts",
        AsyncMock(side_effect=SQLAlchemyError("down")),
    )
    await audience.resumed([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS
    assert TENANT not in audience._live  # type: ignore[reportPrivateUsage]

    monkeypatch.setattr("daimon.adapters.discord.audience_sync.facts_store.publish_facts", real)
    clock.now += timedelta(seconds=60)
    await audience.tick([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "1") == RoomFacts(False, False)


async def test_a_policy_set_or_changed_after_startup_is_picked_up_within_one_tick(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory, internal=(), team=())
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    await audience.tick([server])  # type: ignore[list-item]
    server.fetch_roles.assert_not_awaited()  # type: ignore[attr-defined]
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "no internal roles"

    await set_access_policy(
        db_session,
        tenant_id=TENANT,
        policy=TenantAccessPolicy(internal_role_ids=ROLES, team_role_ids=(str(fake.CORE),)),
    )
    await db_session.commit()
    clock.now += timedelta(seconds=60)
    await audience.tick([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "set"

    # The friend role stops being internal: the friend now counts as an outsider.
    await set_access_policy(
        db_session,
        tenant_id=TENANT,
        policy=TenantAccessPolicy(internal_role_ids=(str(fake.CORE),)),
    )
    await db_session.commit()
    clock.now += timedelta(seconds=60)
    await audience.tick([server])  # type: ignore[list-item]
    assert (await _fact(db_session_factory, clock, "1")).contained, "the friend is an outsider"

    await set_access_policy(
        db_session, tenant_id=TENANT, policy=TenantAccessPolicy(internal_role_ids=())
    )
    await db_session.commit()
    clock.now += timedelta(seconds=60)
    await audience.tick([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS, "retired"


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


class _Recorder:
    """Stands in for AudienceSync or MemberSync on a bot stub and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any) -> None:
            self.calls.append((name, args))

        def sync_call(*args: Any) -> None:
            self.calls.append((name, args))

        return sync_call if name == "schedule" else call

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
    guild = types.SimpleNamespace(id=1, members=[], channels=[])
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


async def test_a_disconnect_stops_the_renewal_and_marks_every_server_unknown() -> None:
    DaimonBot = _bot_class()

    bot = _bot()
    bot._audience_renewal = asyncio.create_task(asyncio.sleep(3600))
    renewal = bot._audience_renewal

    await DaimonBot.on_disconnect(bot)
    await asyncio.sleep(0)

    assert renewal.cancelled()
    assert bot._audience_sync.calls == [("disconnected", ([bot.guild],))]


async def test_a_resume_reconciles_and_restarts_the_renewal_only_with_the_members_intent() -> None:
    DaimonBot = _bot_class()

    bot = _bot()
    renewed = asyncio.Event()

    async def run(_guilds: object) -> None:
        renewed.set()

    bot._audience_sync.run = run
    bot._audience_renewal = stopped = asyncio.create_task(asyncio.sleep(0))
    await stopped
    await DaimonBot.on_resumed(bot)
    await asyncio.wait_for(renewed.wait(), timeout=1)
    assert bot._audience_renewal is not stopped
    assert bot._audience_sync.calls == [("resumed", ([bot.guild],))]

    no_intent = _bot(members_intent=False)
    no_intent._audience_sync.run = run
    await DaimonBot.on_resumed(no_intent)
    assert no_intent._audience_sync.calls == [], "no member cache, no facts"


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


async def test_member_role_channel_and_thread_events_schedule_a_recompute() -> None:
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
    r1 = roles(1)
    cases: list[tuple[str, Callable[[], Any]]] = [
        ("role create", lambda: DaimonBot.on_guild_role_create(bot, role(bot))),
        ("role update", lambda: DaimonBot.on_guild_role_update(bot, role(bot, 0), role(bot, 8))),
        ("role delete", lambda: DaimonBot.on_guild_role_delete(bot, role(bot))),
        ("member join", lambda: DaimonBot.on_member_join(bot, member(bot, r1))),
        (
            "member roles",
            lambda: DaimonBot.on_member_update(bot, member(bot, r1), member(bot, roles(2))),
        ),
        (
            "member leaves",
            lambda: DaimonBot.on_raw_member_remove(
                bot, types.SimpleNamespace(guild_id=1, user=types.SimpleNamespace(id=5))
            ),
        ),
        ("channel create", lambda: DaimonBot.on_guild_channel_create(bot, channel(bot))),
        ("channel delete", lambda: DaimonBot.on_guild_channel_delete(bot, channel(bot))),
        (
            "channel overwrites",
            lambda: DaimonBot.on_guild_channel_update(
                bot, channel(bot), channel(bot, overwrites={1: 1})
            ),
        ),
        (
            "channel renamed",
            lambda: DaimonBot.on_guild_channel_update(bot, channel(bot), channel(bot, name="b")),
        ),
        (
            "thread create",
            lambda: DaimonBot.on_thread_create(bot, types.SimpleNamespace(guild=bot.guild)),
        ),
    ]
    for label, event in cases:
        before = len(bot._audience_sync.calls)
        await event()
        assert bot._audience_sync.calls[before:] == [("schedule", (bot.guild,))], label

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


async def test_a_channel_event_on_the_bot_recomputes_the_real_facts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A real AudienceSync behind the handlers: an event changes a room's facts, and a
    disconnect withdraws them."""
    DaimonBot = _bot_class()

    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    await audience.publish(server, reconciled=True)  # type: ignore[arg-type]
    bot = _bot()
    bot._audience_sync = audience
    bot.guilds = [server]
    server.channels[0].overwrites[fake.target(discord.User, 30)] = discord.PermissionOverwrite(  # type: ignore[attr-defined]
        view_channel=True
    )

    await DaimonBot.on_guild_channel_create(bot, types.SimpleNamespace(guild=server))
    await audience.settle()
    assert (await _fact(db_session_factory, clock, "1")).contained

    bot._audience_renewal = None
    await DaimonBot.on_disconnect(bot)
    assert await _fact(db_session_factory, clock, "1") == UNKNOWN_FACTS
