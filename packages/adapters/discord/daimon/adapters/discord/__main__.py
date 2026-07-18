"""``python -m daimon.adapters.discord`` entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.health import start_liveness_responder
from daimon.core.logging_setup import configure_log_level
from daimon.core.observability import init_sentry
from sentry_sdk.integrations.asyncio import AsyncioIntegration

import discord

log = structlog.get_logger()


async def main() -> None:
    settings = load_settings()
    if settings.discord is None:
        log.info("discord adapter disabled", reason="no bot token")
        sys.exit(0)
    # Configure the JSON log chain BEFORE the first log line so it takes effect.
    # This entrypoint owns the call site.
    configure_log_level(settings.log.level)
    init_sentry(
        dsn=settings.sentry.dsn.get_secret_value() if settings.sentry.dsn else None,
        environment=settings.sentry.environment,
        process="discord",
        release=None,
        traces_sample_rate=settings.sentry.traces_sample_rate,
        integrations=[AsyncioIntegration()],
    )
    async with build_runtime(settings) as runtime:
        intents = discord.Intents.default()
        intents.message_content = True
        bot = DaimonBot(runtime=runtime, intents=intents)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(bot._drain_and_close()))  # pyright: ignore[reportPrivateUsage]  # entrypoint owns the bot lifecycle
        # OB-3 liveness responder on the loop asyncio.run() created (co-location is
        # the property that a hung loop fails the probe and Fly restarts us).
        health_server = await start_liveness_responder(settings.discord.health_port)
        log.info("starting_discord_bot")
        try:
            await bot.start(settings.discord.bot_token.get_secret_value())
        finally:
            health_server.close()
            await health_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
