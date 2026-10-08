"""Discord remembers the names it is handed on messages and interactions, in the background."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.names import remember_guild_user
from daimon.core import platform_names
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.platform_names import KnownName
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.platform_names import get_user_names
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD = 555000000000000001


def _user(*, bot: bool = False, user_id: int = 100000000000000042) -> discord.Member:
    user = MagicMock(spec=discord.Member)
    user.id, user.bot, user.display_name, user.name = user_id, bot, "Maya Chen", "maya"
    return user


async def _stored(session: AsyncSession, user_id: str) -> dict[str, KnownName]:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(GUILD))
    return await get_user_names(
        session, tenant_id=tenant_id, platform="discord", user_ids=[user_id]
    )


async def test_a_guild_users_names_are_remembered(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(GUILD))
    await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="100000000000000042"
    )
    await db_session.commit()

    remember_guild_user(db_session_factory, guild_id=GUILD, user=_user())
    await platform_names.settle()

    assert await _stored(db_session, "100000000000000042") == {
        "100000000000000042": KnownName("Maya Chen", "maya")
    }, "the server display name and the username"


async def test_bots_and_direct_messages_are_not_remembered() -> None:
    def never() -> Any:
        raise AssertionError("nothing to write")

    remember_guild_user(never, guild_id=GUILD, user=_user(bot=True))  # type: ignore[arg-type]
    remember_guild_user(never, guild_id=None, user=_user())  # type: ignore[arg-type]
    await platform_names.settle()


async def test_a_failed_write_never_reaches_the_event() -> None:
    def broken() -> Any:
        raise OSError("database unreachable")

    remember_guild_user(broken, guild_id=GUILD, user=_user())  # type: ignore[arg-type]
    await platform_names.settle()  # logged in the background, nothing raised


async def test_every_interaction_remembers_the_clickers_name(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(GUILD))
    await get_or_create_platform_principal(
        db_session, tenant_id=tenant.id, platform="discord", external_id="100000000000000043"
    )
    await db_session.commit()
    bot = MagicMock()
    bot.runtime.sessionmaker = db_session_factory
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild_id = GUILD
    interaction.user = _user(user_id=100000000000000043)

    await DaimonBot.on_interaction(bot, interaction)
    await platform_names.settle()

    assert await _stored(db_session, "100000000000000043") == {
        "100000000000000043": KnownName("Maya Chen", "maya")
    }
