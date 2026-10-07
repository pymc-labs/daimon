"""An archive the agent asked for in its own thread waits until no turn owns the thread.

The archive runs after the turn's output sweep, which takes seconds; by then a
queued mention may have started a newer turn in the thread. Archiving under it
would fail that turn's card edits, so a busy thread is left open.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.bot import DaimonBot

from .harness import make_bot

_THREAD = 9999


def _bot() -> DaimonBot:
    return make_bot(MagicMock())


def _thread(bot: DaimonBot, seen_claimed: list[bool]) -> MagicMock:
    thread = MagicMock(spec=discord.Thread)
    thread.id = _THREAD

    async def edit(**_kwargs: object) -> None:
        seen_claimed.append(_THREAD in bot._processing)  # pyright: ignore[reportPrivateUsage]

    thread.edit = AsyncMock(side_effect=edit)
    return thread


async def test_an_idle_thread_is_archived_while_held_then_released() -> None:
    bot = _bot()
    seen_claimed: list[bool] = []
    thread = _thread(bot, seen_claimed)

    await bot._archive_when_idle(thread)  # pyright: ignore[reportPrivateUsage]

    thread.edit.assert_awaited_once_with(archived=True)
    assert seen_claimed == [True], "no new turn can start in the thread mid-archive"
    assert _THREAD not in bot._processing, "the thread is released after"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("busy", ["turn_in_flight", "mention_queued", "continuation_waiting"])
async def test_a_thread_a_newer_turn_owns_is_left_open(busy: str) -> None:
    bot = _bot()
    seen_claimed: list[bool] = []
    thread = _thread(bot, seen_claimed)
    if busy == "turn_in_flight":
        bot._processing.add(_THREAD)  # pyright: ignore[reportPrivateUsage]
    elif busy == "mention_queued":
        bot._pending[_THREAD] = [MagicMock(spec=discord.Message)]  # pyright: ignore[reportPrivateUsage]
    else:
        bot._deferred_dispatch[_THREAD] = (uuid.uuid4(), thread, "1")  # pyright: ignore[reportPrivateUsage]

    await bot._archive_when_idle(thread)  # pyright: ignore[reportPrivateUsage]

    thread.edit.assert_not_awaited()


async def test_a_mention_that_arrives_during_the_archive_is_handled_after_it() -> None:
    bot = _bot()
    mention = MagicMock(spec=discord.Message)
    thread = MagicMock(spec=discord.Thread)
    thread.id = _THREAD

    async def edit(**_kwargs: object) -> None:
        # on_message saw the thread held and queued the mention behind it.
        bot._pending[_THREAD] = [mention]  # pyright: ignore[reportPrivateUsage]

    thread.edit = AsyncMock(side_effect=edit)
    bot.on_message = AsyncMock()  # type: ignore[method-assign]

    await bot._archive_when_idle(thread)  # pyright: ignore[reportPrivateUsage]
    await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]

    bot.on_message.assert_awaited_once_with(mention)
    assert _THREAD not in bot._pending, "the queue is handed back, not kept"  # pyright: ignore[reportPrivateUsage]
