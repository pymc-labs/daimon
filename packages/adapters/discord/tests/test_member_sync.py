"""A server's member list follows the gateway: reload, joins, leaves, role changes, removal.
The list never decides a room fact: a reload only asks for the REST reconcile."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.discord.audience_sync import AudienceSync
from daimon.adapters.discord.member_sync import MemberSync, tenant_of
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores import audience_facts as facts_store
from daimon.core.stores import workspace_members as members_store
from daimon.core.stores.access_policy import set_access_policy
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import audience_fakes as fake

_GUILD = 123456789012345678
_OWNER, _MOD, _PLAIN, _BOT = 11, 22, 33, 44


def _member(guild: Any, user_id: int, *, manage: bool = False, bot: bool = False) -> Any:
    member = MagicMock()
    member.id, member.bot, member.guild = user_id, bot, guild
    member.guild_permissions.administrator = False
    member.guild_permissions.manage_guild = manage
    return member


def _guild() -> Any:
    guild = MagicMock()
    guild.id, guild.name, guild.owner_id, guild.chunked = _GUILD, "Guild", _OWNER, False
    guild.unavailable = False
    guild.chunk = AsyncMock()
    guild.members = [
        _member(guild, _OWNER),
        _member(guild, _MOD, manage=True),
        _member(guild, _PLAIN),
        _member(guild, _BOT, bot=True),
    ]
    return guild


async def _rows(factory: async_sessionmaker[AsyncSession]) -> dict[str, bool]:
    async with factory() as session:
        rows = await session.execute(
            text("SELECT platform_user_id, is_admin FROM workspace_members")
        )
        return {user_id: is_admin for user_id, is_admin in rows.tuples()}


async def test_the_list_follows_reload_and_events(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    await db_session.commit()
    sync = MemberSync(db_session_factory, clock=lambda: datetime(2026, 5, 1, tzinfo=UTC))
    guild = _guild()

    await sync.reload(guild)
    guild.chunk.assert_awaited_once()
    assert await _rows(db_session_factory) == {"11": True, "22": True, "33": False}, (
        "humans only; the owner and Manage Server are admins"
    )

    guild.members[2].guild_permissions.administrator = True
    await sync.admins_changed(guild, [guild.members[2]])
    await sync.left(_GUILD, _MOD)
    await sync.joined(_member(guild, 55))
    assert await _rows(db_session_factory) == {"11": True, "33": True, "55": False}, (
        "a role change, a leave and a join each land"
    )

    await sync.removed(_GUILD)
    async with db_session_factory() as session:
        assert await members_store.list_synced(session, tenant_ids=[tenant_of(_GUILD)]) == {}
    assert await _rows(db_session_factory) == {}


async def test_a_server_without_an_install_is_not_listed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await MemberSync(db_session_factory).reload(_guild())

    assert await _rows(db_session_factory) == {}


async def test_a_leave_and_a_demotion_during_a_reload_are_not_undone(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The reload's read still lists the member who left and the admin who lost the role;
    what the events wrote while it ran wins."""
    await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    await db_session.commit()
    sync = MemberSync(db_session_factory, clock=lambda: datetime(2026, 5, 1, tzinfo=UTC))
    guild = _guild()

    async def events_while_chunking(**_: object) -> None:
        await sync.left(_GUILD, _PLAIN)
        demoted = _member(guild, _MOD)
        await sync.admins_changed(guild, [demoted])

    guild.chunk.side_effect = events_while_chunking
    await sync.reload(guild)

    assert await _rows(db_session_factory) == {"11": True, "22": False}, (
        "the stale read neither brings back the leaver nor restores admin"
    )


@pytest.mark.usefixtures("db_clean")
async def test_a_leave_landing_during_the_reload_write_is_written_again(
    db_committing_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leave noted after the reload laid events over its read, while it writes, is
    written once more after the reload commits."""
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with factory.begin() as session:
        await make_tenant(session, platform="discord", workspace_id=str(_GUILD))
    sync = MemberSync(factory, clock=lambda: datetime(2026, 5, 1, tzinfo=UTC))
    real = members_store.replace_members
    leaves: list[asyncio.Task[None]] = []

    async def replace_with_a_leave(session: AsyncSession, **kwargs: Any) -> None:
        leaves.append(asyncio.create_task(sync.left(_GUILD, _PLAIN)))
        await asyncio.sleep(0.05)
        await real(session, **kwargs)

    monkeypatch.setattr(members_store, "replace_members", replace_with_a_leave)
    await sync.reload(_guild())
    await asyncio.gather(*leaves)

    assert "33" not in await _rows(factory), "the leave stands after the reload"


async def test_a_stuck_or_failing_server_leaves_the_rest_to_load(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunking that never finishes times out, and a database error on one server is
    logged; the servers after each still load."""
    stuck, failing, fine = _guild(), _guild(), _guild()
    stuck.id, failing.id, fine.id = 1, 2, 3
    for guild in (stuck, failing, fine):
        await make_tenant(db_session, platform="discord", workspace_id=str(guild.id))
    await db_session.commit()

    async def never(**_: object) -> None:
        await asyncio.Event().wait()

    stuck.chunk.side_effect = never
    real = members_store.replace_members

    async def fail_once(session: AsyncSession, **kwargs: Any) -> None:
        if kwargs["tenant_id"] == tenant_of(failing.id):
            raise OperationalError("INSERT", {}, Exception("connection reset"))
        await real(session, **kwargs)

    monkeypatch.setattr("daimon.adapters.discord.member_sync.CHUNK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(members_store, "replace_members", fail_once)
    await MemberSync(db_session_factory).reload_all([stuck, failing, fine])

    async with db_session_factory() as session:
        synced = await members_store.list_synced(
            session, tenant_ids=[tenant_of(g.id) for g in (stuck, failing, fine)]
        )
    assert set(synced) == {tenant_of(fine.id)}, "only the healthy server loaded, and it did"


async def _with_room_facts(
    db_session: AsyncSession, factory: async_sessionmaker[AsyncSession]
) -> tuple[AudienceSync, MemberSync, asyncio.Event]:
    """An installed server with internal roles, its REST twin, and a gate that holds the
    publisher's next member read."""
    await make_tenant(db_session, platform="discord", workspace_id=str(_GUILD))
    await set_access_policy(
        db_session,
        tenant_id=tenant_of(_GUILD),
        policy=TenantAccessPolicy(internal_role_ids=(str(fake.CORE), str(fake.FRIEND))),
    )
    await db_session.commit()
    server = fake.RestServer(_GUILD)
    audience = AudienceSync(factory, reader=fake.RestDiscord(server).reader(), debounce_s=0)
    gate = asyncio.Event()
    held = [False]

    async def hold(_request: httpx.Request) -> None:
        if held[0]:
            await gate.wait()

    server.hooks["members"] = hold
    sync = MemberSync(factory, clock=lambda: datetime(2026, 5, 1, tzinfo=UTC), audience=audience)
    held[0] = True
    return audience, sync, gate


async def _usable(factory: async_sessionmaker[AsyncSession]) -> bool:
    async with factory() as session:
        status = await facts_store.read_status(
            session, tenant_id=tenant_of(_GUILD), now=datetime.now(UTC)
        )
    return status is not None and status.usable


async def test_a_reload_asks_for_a_reconcile_and_never_publishes_room_facts(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    audience, sync, gate = await _with_room_facts(db_session, db_session_factory)

    await sync.reload(_guild())
    assert await _rows(db_session_factory), "the member list loaded"
    assert not await _usable(db_session_factory), "the gateway cache published nothing"

    gate.set()
    await audience.settle()
    assert await _usable(db_session_factory), "the REST read the reload asked for did"


async def test_a_failed_reload_leaves_the_room_facts_alone(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    audience, sync, gate = await _with_room_facts(db_session, db_session_factory)
    gate.set()
    assert await audience.reconcile(_guild())
    gate.clear()

    stuck = _guild()
    stuck.chunk.side_effect = TimeoutError
    await sync.reload_all([stuck])
    assert await _usable(db_session_factory), "a failed member reload withdraws nothing"
    gate.set()
    await audience.settle()


async def test_a_reload_that_crosses_a_disconnect_reads_nothing_until_the_next_ready(
    db_clean: None, db_committing_engine: AsyncEngine
) -> None:
    """`reload_all` goes on to its next server after the gateway closed. That server's
    reconcile must not publish: events are lost until the next ready or resume."""
    db_session_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with db_session_factory() as db_session:
        audience, sync, gate = await _with_room_facts(db_session, db_session_factory)
    gate.set()
    chunking = asyncio.Event()
    first = _guild()

    async def slow_chunk(**_kwargs: Any) -> None:
        await chunking.wait()

    first.chunk = AsyncMock(side_effect=slow_chunk)
    reloading = asyncio.create_task(sync.reload_all([first, _guild()]))
    while not first.chunk.await_count:  # the first reload asked for its read and chunks
        await asyncio.sleep(0)
    await audience.settle()
    assert await _usable(db_session_factory), "the first server's read published"

    await audience.disconnected([first])
    chunking.set()
    await reloading  # the second server's reload asks for a reconcile now
    await audience.settle()
    assert not await _usable(db_session_factory), "nothing published while disconnected"
    assert not await audience.reconcile(_guild())

    audience.connected()
    assert await audience.reconcile(_guild())
