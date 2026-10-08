"""Discord admission count drives periodic render cadence."""

from unittest.mock import MagicMock

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.config import TurnRenderSettings


async def test_global_turn_claim_stretches_and_release_restores_interval() -> None:
    runtime = MagicMock()
    runtime.settings.turn_render = TurnRenderSettings(stretch_threshold=1, stretched_interval_s=4.0)
    runtime.settings.discord.max_concurrent_turns = None
    bot = DaimonBot(runtime=runtime, intents=discord.Intents.none())
    try:
        assert bot.try_claim_global_turn()
        assert bot._render_interval.current() == 2.0  # pyright: ignore[reportPrivateUsage]
        assert bot.try_claim_global_turn()
        assert bot._render_interval.current() == 4.0  # pyright: ignore[reportPrivateUsage]
        bot.release_global_turn()
        assert bot._render_interval.current() == 2.0  # pyright: ignore[reportPrivateUsage]
        bot.release_global_turn()
    finally:
        await bot.close()
