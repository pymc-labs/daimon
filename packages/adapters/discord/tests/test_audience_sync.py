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
from daimon.adapters.discord import audience_sync
from daimon.adapters.discord.audience_sync import AudienceSync
from daimon.adapters.discord.member_sync import MemberSync, tenant_of
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.audience_facts import UNKNOWN_FACTS, RoomFacts
from daimon.core.stores import audience_facts as store
from daimon.core.stores.access_policy import set_access_policy
from daimon.testing.factories import make_tenant
from discord.state import ChunkRequest, ConnectionState
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
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
    factory: async_sessionmaker[AsyncSession], clock: Clock, *, chunk_timeout_s: float = 60
) -> tuple[AudienceSync, MemberSync]:
    audience = AudienceSync(
        factory,
        clock=clock,
        fresh_for_s=180,
        debounce_s=0,
        reconcile_every_s=300,
        reconcile_limit_s=600,
        chunk_timeout_s=chunk_timeout_s,
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


async def test_the_policy_fence_is_held_from_the_roles_read_to_the_facts_written(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy write takes the same fence exclusively, so it cannot commit between the role
    lists the publisher read and the facts it writes from them. (The test connection is
    shared, so the order of the steps is checked, not the lock itself.)"""
    from daimon.adapters.discord import audience_sync

    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
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
    await audience.tick([_server()])  # type: ignore[list-item]
    assert steps[-3:] == ["fence", "roles", "facts"]


async def test_a_busy_fence_retires_the_facts_and_a_dirty_server_is_never_renewed(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.discord import audience_sync
    from daimon.core.session_fence_retry import FenceUnavailable

    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    await audience.tick([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "published"

    real = audience_sync.lock_policy_writes_shared
    busy = [True]

    async def lock(*args: Any, **kwargs: Any) -> None:
        if busy[0]:
            raise FenceUnavailable(0.0, "advisory")
        await real(*args, **kwargs)

    monkeypatch.setattr(audience_sync, "lock_policy_writes_shared", lock)
    assert await audience.reconcile(server) is False  # type: ignore[arg-type]
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "retired at once"

    # Even a server wrongly thought live is not renewed while dirty.
    audience._live.add(TENANT)  # pyright: ignore[reportPrivateUsage]
    clock.now += timedelta(seconds=60)
    await audience._renew({TENANT: server}, set())  # type: ignore[arg-type]  # pyright: ignore[reportPrivateUsage]
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "no renewal"

    busy[0] = False
    await audience.tick([server])  # type: ignore[list-item]
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False), "recomputed"
    assert TENANT not in audience._dirty  # pyright: ignore[reportPrivateUsage]


async def test_role_lists_that_changed_before_the_publish_retire_the_facts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy write lands after the roles were read and before the publish takes the
    fence: the facts computed from the old lists are never published."""
    from daimon.adapters.discord import audience_sync

    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
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
    await audience.tick([server])  # type: ignore[list-item]
    assert wrote[0]
    assert await _fact(db_session_factory, clock, "2") == UNKNOWN_FACTS, "old lists never publish"
    clock.now += timedelta(seconds=60)
    await audience.tick([server])  # type: ignore[list-item]
    assert (await _fact(db_session_factory, clock, "1")).contained, "the new lists decide"


async def test_a_channel_type_discord_py_does_not_know_does_not_stop_the_facts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fetch_channels` raises `InvalidData` for one unknown type. The reconcile reads the raw
    list, skips that channel, publishes the known rooms and does not raise."""
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server = _server()
    known: dict[str, Any] = {"1": server.channels[0], "2": server.channels[1]}  # type: ignore[attr-defined]
    server.fetch_channels = AsyncMock(  # type: ignore[attr-defined]
        side_effect=discord.InvalidData("Unknown channel type 20 for channel ID 9")
    )
    server._state.http.get_all_guild_channels = AsyncMock(  # type: ignore[attr-defined]
        return_value=[
            {"type": 0, "id": "1"},
            {"type": 20, "id": "9", "name": "never logged"},
            {"type": 0, "id": "2"},
        ]
    )

    def factory(channel_type: int) -> tuple[Any, Any]:
        if channel_type != 0:
            return None, None

        def build(*, guild: object, state: object, data: dict[str, Any]) -> Any:
            return known[data["id"]]

        return build, channel_type

    monkeypatch.setattr(audience_sync, "_guild_channel_factory", factory)

    with capture_logs() as logs:
        assert await audience.reconcile(server)  # type: ignore[arg-type]

    assert await _fact(db_session_factory, clock, "1") == RoomFacts(False, False)
    assert await _fact(db_session_factory, clock, "2") == RoomFacts(True, False)
    assert await _fact(db_session_factory, clock, "9") == UNKNOWN_FACTS, (
        "the skipped room is unknown"
    )
    [skipped] = [log for log in logs if log["event"] == "discord.audience.channel_type_skipped"]
    assert skipped["count"] == 1 and skipped["types"] == [20]
    assert "never logged" not in str(logs)


async def test_a_timed_out_chunk_ends_the_shared_request_and_the_next_one_asks_again(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock, chunk_timeout_s=0.01)
    server: Any = _server()
    # discord.py's own chunk_guild over its real ChunkRequest, with a recording opcode 8.
    state = types.SimpleNamespace(
        _chunk_requests={},
        loop=asyncio.get_running_loop(),
        _get_guild=MagicMock(return_value=None),
        member_cache_flags=types.SimpleNamespace(joined=True),
        chunker=AsyncMock(),
    )
    server._state = state
    server.chunk = lambda **kw: ConnectionState.chunk_guild(state, server, **kw)  # type: ignore[arg-type]
    other = asyncio.create_task(ConnectionState.chunk_guild(state, server, cache=True))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    (shared,) = state._chunk_requests.values()
    assert isinstance(shared, ChunkRequest) and len(shared.waiters) == 1

    assert not await audience.reconcile(server)

    with pytest.raises(TimeoutError):
        await other
    assert not shared.waiters, "no waiter is left pending"
    assert state._chunk_requests == {}
    assert state.chunker.await_count == 1

    assert not await audience.reconcile(server)
    assert state.chunker.await_count == 2, "the next call sends a new request"
    assert state._chunk_requests == {}


async def test_renewal_does_not_stop_while_a_chunk_is_awaited(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server: Any = _server()
    assert await audience.reconcile(server)
    for _ in range(4):
        clock.now += timedelta(seconds=60)
        await audience.tick([server])  # renewed to 420 s
    clock.now += timedelta(seconds=60)  # 300 s: the next reconcile is due
    seen: list[RoomFacts] = []

    async def stalled(**_: object) -> list[Any]:
        clock.now += timedelta(seconds=150)  # 450 s: past the renewal of 240 s
        seen.append(await _fact(db_session_factory, clock, "2"))
        raise TimeoutError

    server.chunk = AsyncMock(side_effect=stalled)
    await audience.tick([server])
    assert seen[0].contained and seen[0].known, "renewed before the wait, within the limit"


async def test_a_publish_that_crosses_a_disconnect_restores_no_trust(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _setup(db_session, db_session_factory)
    clock = Clock()
    audience, _ = _sync(db_session_factory, clock)
    server: Any = _server()
    real = store.publish_facts

    async def disconnect_while_writing(*args: Any, **kwargs: Any) -> int:
        audience._epoch += 1  # pyright: ignore[reportPrivateUsage]  # what disconnected() starts with
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "publish_facts", disconnect_while_writing)
    assert not await audience.reconcile(server)
    assert not audience._live  # pyright: ignore[reportPrivateUsage]
    assert not audience._members_loaded  # pyright: ignore[reportPrivateUsage]
    assert not audience._reconciled  # pyright: ignore[reportPrivateUsage]
    assert not audience._baseline  # pyright: ignore[reportPrivateUsage]
