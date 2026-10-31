from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    EnvironmentSettings,
    Settings,
)
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from discord.ext import commands
from pydantic import PostgresDsn, SecretStr

from .harness import make_bot


async def _ready_bot(
    name: str, guild_ids: list[int]
) -> tuple[DaimonBot, AsyncMock, AsyncMock, MagicMock]:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  # BaseSettings accepts this
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test@localhost/test")),
        anthropic=AnthropicSettings(api_key=SecretStr("test")),
        environment=EnvironmentSettings(name=name),
        discord=DiscordSettings(bot_token=SecretStr("test"), command_guild_ids=guild_ids),
    )
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=MagicMock(),
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),
    )
    bot = make_bot(runtime)
    bot._connection.application_id = 99  # pyright: ignore[reportPrivateUsage]
    bot._startup_replay_started = True  # pyright: ignore[reportPrivateUsage]
    bot._connection._guilds = {  # pyright: ignore[reportPrivateUsage]
        guild_id: _guild(guild_id) for guild_id in (111, 222)
    }
    bot._retire_orphaned_turns = AsyncMock()  # type: ignore[method-assign]
    bot._provision_joined_guild = AsyncMock(return_value=None)  # type: ignore[method-assign]
    bot.tree.sync = AsyncMock()  # type: ignore[method-assign]
    bot.tree.clear_commands = MagicMock()  # type: ignore[method-assign]
    bot.tree.copy_global_to = MagicMock()  # type: ignore[method-assign]
    bot.http.bulk_upsert_global_commands = AsyncMock()  # type: ignore[method-assign]
    with patch(
        "daimon.adapters.discord.bot.list_tenants_by_platform", new_callable=AsyncMock
    ) as tenants:
        tenants.return_value = []
        await bot.on_ready()
    return (
        bot,
        bot.tree.sync,  # type: ignore[return-value]
        bot.http.bulk_upsert_global_commands,  # type: ignore[return-value]
        bot.tree.copy_global_to,  # type: ignore[return-value]
    )


def _guild(guild_id: int) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = str(guild_id)
    guild.unavailable = False
    return guild


@pytest.mark.asyncio
async def test_production_still_syncs_global_commands() -> None:
    bot, sync, global_overwrite, copy = await _ready_bot("", [])

    sync.assert_any_await()
    assert sync.await_count == 1
    global_overwrite.assert_not_awaited()
    copy.assert_not_called()
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_production_clears_global_and_scopes_commands_to_allowlist() -> None:
    bot, sync, global_overwrite, copy = await _ready_bot("staging", [111])

    assert all("guild" in call.kwargs for call in sync.await_args_list)
    assert {call.kwargs["guild"].id for call in sync.await_args_list} == {111, 222}
    global_overwrite.assert_awaited_once_with(99, payload=[])
    copy.assert_called_once()
    assert copy.call_args.kwargs["guild"].id == 111
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_empty_non_production_allowlist_registers_no_commands() -> None:
    bot, sync, global_overwrite, copy = await _ready_bot("staging", [])

    global_overwrite.assert_awaited_once_with(99, payload=[])
    assert {call.kwargs["guild"].id for call in sync.await_args_list} == {111, 222}
    copy.assert_not_called()
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_allowlisted_messages_and_interactions_are_ignored() -> None:
    bot, _, _, _ = await _ready_bot("staging", [111])
    with patch("daimon.adapters.discord.bot.remember_guild_user") as remember:
        await bot.on_message(SimpleNamespace(guild=SimpleNamespace(id=222)))  # type: ignore[arg-type]
        await bot.on_interaction(SimpleNamespace(guild_id=222))  # type: ignore[arg-type]
    remember.assert_not_called()
    # The SDK otherwise routes slash commands, buttons, and modals before
    # on_interaction; invalid stubs here prove rejection before SDK parsing.
    parse = cast(
        Callable[[dict[str, object]], None],
        bot._connection.parsers["INTERACTION_CREATE"],  # pyright: ignore[reportPrivateUsage]
    )
    with patch("discord.state.Interaction") as make_interaction:
        for kind in (2, 3, 5):
            parse({"type": kind, "guild_id": "222"})
        make_interaction.assert_not_called()
    with patch.object(commands.Bot, "dispatch") as inherited_dispatch:
        bot.dispatch("message", SimpleNamespace(guild=SimpleNamespace(id=222)))
        bot.dispatch("interaction", SimpleNamespace(guild_id=222))
        inherited_dispatch.assert_not_called()
        bot.dispatch("message", SimpleNamespace(guild=SimpleNamespace(id=111)))
        inherited_dispatch.assert_called_once()
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_production_dms_and_their_buttons_reach_the_listeners() -> None:
    bot, _, _, _ = await _ready_bot("lane-preview", [111])
    with patch.object(commands.Bot, "dispatch") as inherited_dispatch:
        bot.dispatch("message", SimpleNamespace(guild=None))
        bot.dispatch("interaction", SimpleNamespace(guild_id=None, guild=None))
    assert [call.args[0] for call in inherited_dispatch.call_args_list] == [
        "message",
        "interaction",
    ], "a lane bot must pass guildless DM events to the direct-message cog"
    parse = cast(
        Callable[[dict[str, object]], None],
        bot._connection.parsers["INTERACTION_CREATE"],  # pyright: ignore[reportPrivateUsage]
    )
    with patch("discord.state.Interaction") as make_interaction:
        parse({"type": 3, "data": {"custom_id": "dm-switch", "component_type": 2}})
        make_interaction.assert_called_once()
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_production_dms_reach_only_allowlisted_tenants() -> None:
    lane, _, _, _ = await _ready_bot("lane-preview", [111])
    production, _, _, _ = await _ready_bot("", [])
    allowed = SimpleNamespace(external_id="111")
    other = SimpleNamespace(external_id="222")
    assert lane.allows_tenant(allowed)  # type: ignore[arg-type]
    assert not lane.allows_tenant(other), "a lane must not answer DMs for a guild off its list"  # type: ignore[arg-type]
    assert not lane.allows_tenant(None)
    assert production.allows_tenant(other)  # type: ignore[arg-type]
    await lane.close()  # type: ignore[attr-defined]
    await production.close()  # type: ignore[attr-defined]
