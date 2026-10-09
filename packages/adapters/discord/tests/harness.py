"""Shared Discord-adapter test harness: the bot builder every bot test
used to copy verbatim.

Runtime builders stay per-file on purpose: each pins the settings
attributes its tests read, and a superset would change truthiness branches.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import TurnQueueSettings
from daimon.core.turn_queue import TurnQueue


def make_bot(runtime: DiscordRuntime) -> DaimonBot:
    """A `DaimonBot` on `runtime` whose gateway user is a stub that is
    mentioned in every message, so `on_message` reaches orchestration."""
    intents = discord.Intents.default()
    intents.message_content = True
    bot = DaimonBot(runtime=runtime, intents=intents)
    if not isinstance(runtime.settings.turn_queue, TurnQueueSettings):
        # Mocked settings: the default queue, no process-wide cap. Tests that
        # need a cap or a bound set it on `bot.turn_queue`.
        bot.turn_queue = TurnQueue.from_settings(TurnQueueSettings(), platform="discord")
    bot._connection.user = MagicMock(spec=discord.ClientUser)  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.id = 999  # pyright: ignore[reportPrivateUsage]
    bot._connection.user.mentioned_in = MagicMock(return_value=True)  # pyright: ignore[reportPrivateUsage]
    return bot
