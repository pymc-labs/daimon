from __future__ import annotations

from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.adapters.discord.staging_gate import NoticeLimiter
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
async def test_non_allowlisted_messages_interactions_and_dms_are_ignored() -> None:
    bot, _, _, _ = await _ready_bot("staging", [111])
    with patch("daimon.adapters.discord.bot.remember_guild_user") as remember:
        await bot.on_message(SimpleNamespace(guild=SimpleNamespace(id=222)))  # type: ignore[arg-type]
        await bot.on_message(SimpleNamespace(guild=None))  # type: ignore[arg-type]
        await bot.on_interaction(SimpleNamespace(guild_id=222))  # type: ignore[arg-type]
        await bot.on_interaction(SimpleNamespace(guild_id=None))  # type: ignore[arg-type]
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
            parse({"type": kind})
        make_interaction.assert_not_called()
    with patch.object(commands.Bot, "dispatch") as inherited_dispatch:
        bot.dispatch("message", SimpleNamespace(guild=SimpleNamespace(id=222)))
        bot.dispatch("interaction", SimpleNamespace(guild_id=222))
        bot.dispatch("message", SimpleNamespace(guild=None))
        inherited_dispatch.assert_not_called()
        bot.dispatch("message", SimpleNamespace(guild=SimpleNamespace(id=111)))
        inherited_dispatch.assert_called_once()
    await bot.close()  # type: ignore[attr-defined]


def _dm(author_id: int) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.author = SimpleNamespace(id=author_id, bot=False)
    message.webhook_id = None
    message.guild = None
    message.mentions = []
    message.channel = MagicMock(spec=discord.DMChannel)
    message.channel.send = AsyncMock()
    return message


def _guild_message(guild_id: int, channel_id: int, *, mentions_bot: bool) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.author = SimpleNamespace(id=5, bot=False)
    message.webhook_id = None
    message.guild = SimpleNamespace(id=guild_id)
    message.mentions = [SimpleNamespace(id=4242)] if mentions_bot else []
    message.channel = MagicMock(spec=discord.TextChannel)
    message.channel.id = channel_id
    message.channel.send = AsyncMock()
    return message


async def _staging_bot(
    *, member: bool | Exception, name: str = "staging"
) -> tuple[DaimonBot, list[Coroutine[Any, Any, object]]]:
    bot, _, _, _ = await _ready_bot(name, [111])
    bot._connection.user = SimpleNamespace(id=4242)  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    allowed = cast(MagicMock, bot.get_guild(111))
    allowed.get_member = MagicMock(return_value=None)
    if isinstance(member, Exception):
        allowed.fetch_member = AsyncMock(side_effect=member)
    elif member:
        allowed.fetch_member = AsyncMock(return_value=SimpleNamespace(id=7))
    else:
        allowed.fetch_member = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "Unknown Member")
        )
    spawned: list[Coroutine[Any, Any, object]] = []
    bot._spawn = spawned.append  # type: ignore[method-assign,assignment]
    return bot, spawned


async def _run(spawned: list[Coroutine[Any, Any, object]]) -> None:
    while spawned:
        await spawned.pop(0)


@pytest.mark.asyncio
async def test_staging_dm_from_a_member_of_an_allowed_guild_is_handled() -> None:
    bot, spawned = await _staging_bot(member=True)
    message = _dm(7)
    with patch.object(commands.Bot, "dispatch") as inherited_dispatch:
        bot.dispatch("message", message)
        inherited_dispatch.assert_not_called()
        await _run(spawned)
    inherited_dispatch.assert_called_once_with("message", message)
    message.channel.send.assert_not_awaited()
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_staging_dm_from_a_stranger_is_told_once_and_never_handled() -> None:
    bot, spawned = await _staging_bot(member=False)
    first, again = _dm(7), _dm(7)
    with patch.object(commands.Bot, "dispatch") as inherited_dispatch:
        bot.dispatch("message", first)
        bot.dispatch("message", again)
        await _run(spawned)
    inherited_dispatch.assert_not_called()
    first.channel.send.assert_awaited_once()
    text = first.channel.send.await_args.args[0]
    assert text == "Daimon isn't enabled for you yet. Ask the team running Daimon to enable it."
    again.channel.send.assert_not_awaited()
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_staging_dm_whose_membership_is_unknown_fails_closed() -> None:
    bot, spawned = await _staging_bot(member=discord.HTTPException(MagicMock(status=500), "boom"))
    message = _dm(7)
    with patch.object(commands.Bot, "dispatch") as inherited_dispatch:
        bot.dispatch("message", message)
        await _run(spawned)
    inherited_dispatch.assert_not_called()
    message.channel.send.assert_not_awaited()
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mention_in_an_unlisted_guild_gets_one_line_per_channel_per_hour() -> None:
    bot, spawned = await _staging_bot(member=False)
    bot.runtime.settings.discord.staging_contact = "Carlos"  # type: ignore[union-attr]
    first = _guild_message(222, 50, mentions_bot=True)
    again = _guild_message(222, 50, mentions_bot=True)
    other_channel = _guild_message(222, 51, mentions_bot=True)
    unmentioned = _guild_message(222, 52, mentions_bot=False)
    with patch.object(commands.Bot, "dispatch") as inherited_dispatch:
        for message in (first, again, other_channel, unmentioned):
            bot.dispatch("message", message)
        await _run(spawned)
    inherited_dispatch.assert_not_called()
    first.channel.send.assert_awaited_once()
    assert first.channel.send.await_args.args[0] == (
        "Daimon isn't enabled in this server yet. Ask Carlos to enable it."
    )
    again.channel.send.assert_not_awaited()
    other_channel.channel.send.assert_awaited_once()
    unmentioned.channel.send.assert_not_awaited()
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_install_to_an_unlisted_guild_posts_one_line_and_provisions_nothing() -> None:
    bot, spawned = await _staging_bot(member=False)
    guild = _guild(333)
    channel = MagicMock(spec=discord.TextChannel)
    channel.send = AsyncMock()
    cast(AsyncMock, bot._provision_joined_guild).reset_mock()  # pyright: ignore[reportPrivateUsage]
    with (
        patch("daimon.adapters.discord.bot._pick_post_channel", return_value=channel),
        patch.object(commands.Bot, "dispatch") as inherited_dispatch,
    ):
        bot.dispatch("guild_join", guild)
        await _run(spawned)
    inherited_dispatch.assert_not_called()
    channel.send.assert_awaited_once()
    assert "isn't enabled in this server yet" in channel.send.await_args.args[0]
    cast(AsyncMock, bot._provision_joined_guild).assert_not_awaited()  # pyright: ignore[reportPrivateUsage]
    await bot.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_production_dispatches_dms_without_a_membership_check() -> None:
    bot, spawned = await _staging_bot(member=False, name="")
    message = _dm(7)
    with patch.object(commands.Bot, "dispatch") as inherited_dispatch:
        bot.dispatch("message", message)
    inherited_dispatch.assert_called_once_with("message", message)
    assert not spawned
    await bot.close()  # type: ignore[attr-defined]


def test_notice_limiter_allows_one_per_key_per_cooldown() -> None:
    now = [0.0]
    limiter = NoticeLimiter(cooldown_s=3600, limit=2, clock=lambda: now[0])
    assert limiter.allow(1) is True
    assert limiter.allow(1) is False
    now[0] = 3599
    assert limiter.allow(1) is False
    now[0] = 3600
    assert limiter.allow(1) is True, "an hour later the channel may be told again"
    limiter.forget(1)
    assert limiter.allow(1) is True
    limiter.allow(2)
    limiter.allow(3)
    assert limiter.allow(1) is True, "the oldest key is forgotten past the limit"
