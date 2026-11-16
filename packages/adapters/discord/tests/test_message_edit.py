"""An edit that adds a mention of the bot answers; every other edit stays quiet."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import ThreadNamingSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.deps import build_turn_deps
from daimon.testing.settings_mock import mock_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot

_BOT_ID = 999


def _snowflake(age: timedelta = timedelta(minutes=5)) -> int:
    """A message id minted `age` ago."""
    return discord.utils.time_snowflake(datetime.now(UTC) - age)


def _bot(sessionmaker: async_sessionmaker[AsyncSession]) -> DaimonBot:
    settings = mock_settings()
    settings.defaults_root = MagicMock()
    settings.discord = MagicMock()
    settings.thread_naming = ThreadNamingSettings(enabled=False)
    anthropic = AsyncMock()
    resolver_cache = new_resolver_cache()
    deployment_default = DeploymentDefault()
    runtime = DiscordRuntime(
        settings=settings,
        anthropic=anthropic,
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=deployment_default,
        resolver_cache=resolver_cache,
        turn_deps=build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=None,
        ),
    )
    return make_bot(runtime)


def _edit(
    *,
    mentions: list[int],
    bot_author: bool = False,
    before: list[int] | None = None,
    age: timedelta = timedelta(minutes=5),
    edited: bool = True,
    before_content: str | None = None,
    content: str = "hey <@999>",
) -> discord.RawMessageUpdateEvent:
    message_id = _snowflake(age)
    data: dict[str, Any] = {
        "id": str(message_id),
        "content": content,
        "edited_timestamp": datetime.now(UTC).isoformat() if edited else None,
        "channel_id": "6",
        "guild_id": "7",
        "author": {"id": "42", "bot": bot_author},
        "mentions": [{"id": str(user)} for user in mentions],
    }
    payload = MagicMock(spec=discord.RawMessageUpdateEvent)
    payload.message_id, payload.channel_id, payload.guild_id = message_id, 6, 7
    payload.data = data
    payload.cached_message = (
        None
        if before is None
        else MagicMock(
            mentions=[MagicMock(id=user) for user in before],
            content=before_content if before_content is not None else "hey",
        )
    )
    return payload


async def _run_edit(
    bot: DaimonBot,
    payload: discord.RawMessageUpdateEvent,
    *,
    fetched_bot: bool = False,
    thread: object | None = None,
) -> AsyncMock:
    """Deliver the edit; return the `on_message` mock that stands in for a turn."""
    message = MagicMock(spec=discord.Message)
    message.author = MagicMock(bot=fetched_bot)
    message.thread = thread
    channel = MagicMock(spec=discord.TextChannel)
    channel.fetch_message = AsyncMock(return_value=message)
    on_message = AsyncMock()
    with (
        patch.object(bot, "_channel_by_id", AsyncMock(return_value=channel)),
        patch.object(bot, "on_message", on_message),
    ):
        await bot.on_raw_message_edit(payload)
    return on_message


async def test_an_edit_that_adds_a_mention_runs_one_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The edited message is fetched and handled once, as if it had just arrived."""
    on_message = await _run_edit(_bot(db_session_factory), _edit(mentions=[_BOT_ID], before=[]))
    assert on_message.await_count == 1, "one turn for the edit that added the mention"


async def test_an_edit_of_a_message_that_already_mentioned_the_bot_is_ignored(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A message answered on arrival is not answered again when it is edited."""
    on_message = await _run_edit(
        _bot(db_session_factory), _edit(mentions=[_BOT_ID], before=[_BOT_ID])
    )
    assert on_message.await_count == 0, "no second turn for an answered message"


async def test_an_edit_without_a_mention_of_the_bot_is_ignored(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mentioning someone else, or no one, does not address the bot."""
    bot = _bot(db_session_factory)
    for mentions in ([], [123]):
        on_message = await _run_edit(bot, _edit(mentions=mentions))
        assert on_message.await_count == 0, f"no turn when the edit mentions {mentions}"


async def test_an_edit_by_a_bot_author_is_ignored(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Another bot's edited message never starts a turn."""
    on_message = await _run_edit(
        _bot(db_session_factory), _edit(mentions=[_BOT_ID], bot_author=True)
    )
    assert on_message.await_count == 0, "no turn for a bot's edit"


async def test_an_old_message_is_not_answered_after_an_edit(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Past the ledger's memory, an uncached message may have been answered: it stays quiet."""
    old = _edit(mentions=[_BOT_ID], age=timedelta(hours=3))
    on_message = await _run_edit(_bot(db_session_factory), old)
    assert on_message.await_count == 0, "no turn for an edited message older than the ledger"


async def test_an_update_that_is_not_a_content_edit_is_ignored(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pin or flag update has no edit time; unchanged text is not a new request."""
    bot = _bot(db_session_factory)
    pinned = await _run_edit(bot, _edit(mentions=[_BOT_ID], edited=False))
    assert pinned.await_count == 0, "no turn for an update with no edit time"
    same = await _run_edit(bot, _edit(mentions=[_BOT_ID], before=[], before_content="hey <@999>"))
    assert same.await_count == 0, "no turn when the text did not change"


async def test_a_message_that_already_has_a_thread_is_ignored(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A thread on the message means it was answered, or another bot took it."""
    on_message = await _run_edit(
        _bot(db_session_factory), _edit(mentions=[_BOT_ID]), thread=MagicMock(spec=discord.Thread)
    )
    assert on_message.await_count == 0, "no second answer where a thread exists"
