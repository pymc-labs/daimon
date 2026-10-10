"""on_message: an unmentioned reply in daimon's own thread gets no turn, at most one hint.

Same bot/message fakes as `test_thread_participation_routing.py`, with
`_handle_mention` stubbed so a turn that should not start is visible as a
recorded call. The hint is a 🔔 reaction on the reply, once per thread per
cooldown, only in threads daimon opened, and never while the thread is
followed.
"""

from __future__ import annotations

import itertools
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest_asyncio
from daimon.adapters.discord.bot import UNMENTIONED_REPLY_HINT_EMOJI, DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import McpSettings, ThreadParticipationSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import thread_participation as store
from daimon.core.thread_participation import ParticipationMode, ParticipationScope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot

GUILD_ID = 123456
BOT_ID = 999
THREAD_ID = 789
PARENT_ID = 700
HUMAN_ID = 111
OTHER_HUMAN_ID = 222
QA_BOT_ID = 333


def _make_runtime(
    sessionmaker: async_sessionmaker[AsyncSession], *, hint: bool = True
) -> DiscordRuntime:
    settings = MagicMock()
    settings.mcp = McpSettings()
    discord_settings = MagicMock()
    discord_settings.max_concurrent_turns_per_tenant = 100
    discord_settings.thread_open_notice_after_s = 3.0
    discord_settings.qa_bot_user_ids = (str(QA_BOT_ID),)
    discord_settings.bot_display_name = "daimon"
    discord_settings.unmentioned_reply_hint = hint
    discord_settings.unmentioned_reply_hint_cooldown_h = 24.0
    settings.discord = discord_settings
    settings.billing.markup = Decimal("1.0")
    settings.thread_participation = ThreadParticipationSettings(
        mode=ParticipationMode.OFF, quiet_seconds=0.2
    )
    return DiscordRuntime(
        settings=settings,
        anthropic=AsyncMock(),
        sessionmaker=sessionmaker,
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # _handle_mention stubbed
    )


def _make_thread(*, owner_id: int = BOT_ID, thread_id: int = THREAD_ID) -> Any:
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    thread.parent_id = PARENT_ID
    thread.owner_id = owner_id
    return thread


_next_message_id = itertools.count(1000)


def _reply(
    thread: Any,
    *,
    author_id: int = HUMAN_ID,
    author_is_bot: bool = False,
    mentions: tuple[int, ...] = (),
) -> Any:
    message = MagicMock(spec=discord.Message)
    message.id = next(_next_message_id)
    message.content = "and what about the second one?"
    message.type = discord.MessageType.default
    message.webhook_id = None
    message.reference = None
    message.author = MagicMock()
    message.author.bot = author_is_bot
    message.author.id = author_id
    message.guild = MagicMock(spec=discord.Guild)
    message.guild.id = GUILD_ID
    message.channel = thread
    message.add_reaction = AsyncMock()
    message.attachments = []
    message.mentions = [SimpleNamespace(id=uid) for uid in mentions]
    message.role_mentions = []
    message.mention_everyone = False
    return message


def _stub_turn(bot: DaimonBot) -> list[Any]:
    calls: list[Any] = []

    async def stub(message: Any, guild_id: str, tenant_id: uuid.UUID, **kwargs: Any) -> None:
        calls.append(message)

    bot._handle_mention = stub  # type: ignore[method-assign]
    return calls


def _reacted(message: Any) -> bool:
    return any(
        call.args == (UNMENTIONED_REPLY_HINT_EMOJI,) for call in message.add_reaction.call_args_list
    )


@pytest_asyncio.fixture
async def tenant_id(db_session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    result = await provision_tenant(
        db_session_factory,
        platform="discord",
        workspace_id=str(GUILD_ID),
        signup_credit=Decimal("10"),
    )
    return result.tenant_id


async def test_unmentioned_reply_starts_no_turn_and_gets_one_hint(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    bot = make_bot(_make_runtime(db_session_factory))
    turns = _stub_turn(bot)
    thread = _make_thread()

    first = _reply(thread)
    second = _reply(thread)
    await bot.on_message(first)
    await bot.on_message(second)

    assert turns == [], "an unmentioned reply is never answered"
    assert _reacted(first), "the first unmentioned reply in daimon's thread gets the hint"
    assert not _reacted(second), "the hint is not repeated in the same thread"


async def test_hint_is_per_thread(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    bot = make_bot(_make_runtime(db_session_factory))
    _stub_turn(bot)

    here = _reply(_make_thread())
    elsewhere = _reply(_make_thread(thread_id=THREAD_ID + 1))
    await bot.on_message(here)
    await bot.on_message(elsewhere)

    assert _reacted(here) and _reacted(elsewhere), "each thread gets its own one hint"


async def test_no_hint_outside_daimons_threads_or_between_people(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    bot = make_bot(_make_runtime(db_session_factory))
    turns = _stub_turn(bot)

    human_thread = _reply(_make_thread(owner_id=OTHER_HUMAN_ID))
    to_a_person = _reply(_make_thread(), mentions=(OTHER_HUMAN_ID,))
    other_bot = _reply(_make_thread(), author_id=444, author_is_bot=True)
    for message in (human_thread, to_a_person, other_bot):
        await bot.on_message(message)

    assert turns == [], "none of these start a turn"
    assert not _reacted(human_thread), "a thread daimon did not open gets no hint"
    assert not _reacted(to_a_person), "a reply addressed to another person gets no hint"
    assert not _reacted(other_bot), "a bot gets no hint"


async def test_a_mention_in_daimons_thread_is_answered_once_without_a_hint(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    bot = make_bot(_make_runtime(db_session_factory))
    turns = _stub_turn(bot)

    mention = _reply(_make_thread(), mentions=(BOT_ID,))
    await bot.on_message(mention)

    assert turns == [mention], "a mention starts exactly one turn"
    assert not _reacted(mention), "a mention is not hinted at"


async def test_no_hint_when_the_setting_is_off(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    bot = make_bot(_make_runtime(db_session_factory, hint=False))
    _stub_turn(bot)

    message = _reply(_make_thread())
    await bot.on_message(message)

    assert not _reacted(message), "the setting turns the hint off"


async def test_no_hint_in_a_followed_thread(
    db_session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    async with db_session_factory() as session, session.begin():
        await store.set_participation_mode(
            session,
            tenant_id=tenant_id,
            platform="discord",
            scope=ParticipationScope.THREAD,
            scope_id=str(THREAD_ID),
            mode=ParticipationMode.ON,
        )
    bot = make_bot(_make_runtime(db_session_factory))
    _stub_turn(bot)
    bot._maybe_participate = AsyncMock()  # type: ignore[method-assign]

    message = _reply(_make_thread())
    await bot.on_message(message)

    assert not _reacted(message), (
        "a followed thread may still answer unprompted, so 'mention me' would be wrong"
    )
