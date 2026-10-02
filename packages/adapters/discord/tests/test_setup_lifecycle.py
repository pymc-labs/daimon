"""Raw gateway lifecycle events maintain bindings even with an empty thread cache."""

from __future__ import annotations

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.thread_agent_bindings import create_binding, get_binding
from daimon.core.turn.deps import build_turn_deps
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_raw_archive_reopen_delete_preserve_setup_identities(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="discord", workspace_id="111")
        await create_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="222",
            thread_id="333",
            responder_ma_agent_id="agent_daimon",
            responder_name="Daimon",
            configuration_target_ma_agent_id="agent_specialist",
            configuration_target_name="Specialist",
        )
    settings = Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://test:test@localhost/daimon_test"},
            "anthropic": {"api_key": "test"},
        }
    )
    anthropic = build_stub_anthropic()
    defaults = DeploymentDefault()
    cache = new_resolver_cache()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=db_session_factory,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=defaults,
        resolver_cache=cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            db_session_factory,
            deployment_default=defaults,
            resolver_cache=cache,
            billing_config=None,
        ),
    )
    bot = DaimonBot(runtime=runtime, intents=discord.Intents.none())
    for archived in (True, False):
        await bot.on_raw_thread_update(
            discord.RawThreadUpdateEvent(
                {
                    "id": "333",
                    "guild_id": "111",
                    "parent_id": "222",
                    "type": 11,
                    "thread_metadata": {
                        "archived": archived,
                        "locked": archived,
                        "auto_archive_duration": 60,
                        "archive_timestamp": "2026-09-01T00:00:00Z",
                    },
                }
            )
        )
        async with db_session_factory() as session:
            binding = await get_binding(
                session,
                tenant_id=tenant.id,
                platform="discord",
                parent_channel_id="222",
                thread_id="333",
            )
        assert binding is not None and binding.archived is archived, (
            "raw archive/reopen events must work without cached threads"
        )
        assert binding.locked is archived, "locking and reopening must persist"
        assert binding.responder_ma_agent_id == "agent_daimon", (
            "lifecycle must retain responder identity"
        )
        assert binding.configuration_target_ma_agent_id == "agent_specialist", (
            "lifecycle must retain target identity"
        )
    await bot.on_raw_thread_delete(
        discord.RawThreadDeleteEvent(
            {
                "id": "333",
                "guild_id": "111",
                "parent_id": "222",
                "type": 11,
            }
        )
    )
    async with db_session_factory() as session:
        binding = await get_binding(
            session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="222",
            thread_id="333",
        )
    assert binding is not None and binding.deleted, (
        "raw deletion must tombstone the original binding"
    )
    assert binding.configuration_target_ma_agent_id == "agent_specialist", (
        "deletion must retain identity for honest diagnostics"
    )
