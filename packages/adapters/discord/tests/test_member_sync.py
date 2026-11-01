"""A server's member list follows the gateway: reload, joins, leaves, role changes, removal."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from daimon.adapters.discord.member_sync import MemberSync, tenant_of
from daimon.core.stores import workspace_members as members_store
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
