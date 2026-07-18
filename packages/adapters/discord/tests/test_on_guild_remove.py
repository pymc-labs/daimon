"""Tests for on_guild_remove soft-archive.

Invariant guild_remove_archive: a remove stamps archived_at=now() and NEVER deletes the
Tenant row (RESEARCH Anti-Pattern 3 / PITFALLS #7).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import get_tenant_liveness
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    return DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


def _make_bot(runtime: DiscordRuntime) -> DaimonBot:
    intents = discord.Intents.default()
    bot = DaimonBot(runtime=runtime, intents=intents)
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    return bot


def _make_guild(guild_id: int) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "Leaving Guild"
    return guild


async def test_on_guild_remove_soft_archives_without_delete(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guild_id = "666000111"
    await provision_tenant(db_session_factory, platform="discord", workspace_id=guild_id)

    runtime = _make_runtime(db_session_factory)
    bot = _make_bot(runtime)

    await bot.on_guild_remove(_make_guild(int(guild_id)))

    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    tr = await get_tenant_liveness(db_session_factory, derived)
    assert tr is not None, "row must still exist after remove (soft-archive, no delete)"
    assert tr.archived_at is not None, "on_guild_remove must stamp archived_at"

    # Rows are still present (no delete).
    from daimon.core._models import Tenant  # test-only ORM peek

    tenant_count = (
        await db_session.execute(
            select(func.count()).select_from(Tenant).where(Tenant.id == derived)
        )
    ).scalar_one()
    assert tenant_count == 1, "Tenant row must NOT be deleted on remove"
