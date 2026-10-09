"""Remember the names Discord hands us on messages and interactions.

`/billing` names top spenders from the member cache or a fetch; when neither
answers (they left, the fetch timed out) it falls back to what was stored
here. Recording is best-effort and in the background
(`daimon.core.platform_names`): it never delays or fails the event.
"""

from __future__ import annotations

from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.platform_names import remember_user_name
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord


def remember_guild_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    guild_id: int | None,
    user: discord.User | discord.Member,
) -> None:
    """Store a guild user's server display name and username; bots and DMs are skipped."""
    if guild_id is None or user.bot:
        return
    remember_user_name(
        sessionmaker,
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)),
        platform="discord",
        user_id=str(user.id),
        display_name=user.display_name,
        handle=user.name,
    )
