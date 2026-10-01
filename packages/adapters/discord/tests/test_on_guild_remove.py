"""Tests for on_guild_remove soft-archive.

Invariant guild_remove_archive: a remove stamps archived_at=now() and NEVER deletes the
Tenant row (RESEARCH Anti-Pattern 3 / PITFALLS #7).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import get_tenant, get_tenant_liveness
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot


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
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # on_guild_remove never runs a turn
    )


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
    bot = make_bot(runtime)

    await bot.on_guild_remove(_make_guild(int(guild_id)))

    derived = derive_tenant_uuid(platform="discord", workspace_id=guild_id)
    tr = await get_tenant_liveness(db_session_factory, derived)
    assert tr is not None, "row must still exist after remove (soft-archive, no delete)"
    assert tr.archived_at is not None, "on_guild_remove must stamp archived_at"

    # Row is still present (no delete).
    still_present = await get_tenant(db_session, derived)
    assert still_present is not None, "Tenant row must NOT be deleted on remove"
