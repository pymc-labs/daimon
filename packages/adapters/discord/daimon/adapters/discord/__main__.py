"""``python -m daimon.adapters.discord`` entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from typing import cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.health import start_liveness_responder
from daimon.core.logging_setup import configure_logging
from daimon.core.observability import init_sentry
from daimon.core.runtime_health import runtime_health
from daimon.core.stores.worker_ownership import OWNER_HARD_EXIT_S
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sqlalchemy.ext.asyncio import AsyncEngine

import discord

log = structlog.get_logger()


async def drain_on_owner_loss(bot: DaimonBot) -> None:
    await bot.runtime.ownership_lost.wait()
    deadline = asyncio.get_running_loop().call_later(OWNER_HARD_EXIT_S, os._exit, 1)
    try:
        await bot._drain_and_close()  # pyright: ignore[reportPrivateUsage]  # entrypoint owns shutdown
    finally:
        # Cancellation cleanup can leave a task running after close(). The
        # heartbeat grace includes this hard exit deadline.
        deadline.cancel()
        os._exit(1)


async def start_when_leased(
    bot: DaimonBot, token: str, *, stop: asyncio.Event, started: asyncio.Event
) -> None:
    """Connect to the gateway only once this process holds the gateway lease.

    A standby (the new container during a deploy overlap) is fully warm but
    idle here; the old process releases the lease after closing its gateway.
    `stop` ends the wait without touching the lease; `started` is set the moment
    the gateway connection begins, so a later stop must drain instead.
    """
    lease = bot.runtime.gateway_lease
    if lease is not None:
        acquire = asyncio.create_task(
            lease.acquire(bot.runtime.ownership_lost), name="discord.gateway_lease"
        )
        stopper = asyncio.create_task(stop.wait(), name="discord.gateway_wait_stop")
        try:
            await asyncio.wait({acquire, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopper.cancel()
            if not acquire.done():
                acquire.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await acquire
        if acquire.cancelled():
            return
        acquire.result()
        if stop.is_set():
            await lease.release()
            return
    if stop.is_set():
        return
    started.set()
    await bot.start(token)


async def main() -> None:
    settings = load_settings()
    if settings.discord is None:
        log.info("discord adapter disabled", reason="no bot token")
        sys.exit(0)
    # Configure the JSON log chain BEFORE the first log line so it takes effect.
    # This entrypoint owns the call site.
    configure_logging(settings.log.level)
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
        bot.wake_poller_enabled = True
        loop = asyncio.get_running_loop()
        stop, started = asyncio.Event(), asyncio.Event()

        def on_signal() -> None:
            stop.set()
            # Before the gateway connection there is nothing to drain.
            if started.is_set():
                asyncio.create_task(bot._drain_and_close())  # pyright: ignore[reportPrivateUsage]  # entrypoint owns the bot lifecycle

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, on_signal)
        # OB-3 liveness responder on the loop asyncio.run() created (co-location is
        # the property that a hung loop fails the probe and Fly restarts us).
        health_server = await start_liveness_responder(settings.discord.health_port)
        log.info("starting_discord_bot")
        owner_drain = asyncio.create_task(drain_on_owner_loss(bot), name="discord.owner_loss_drain")
        try:
            async with runtime_health(
                "discord",
                cast(AsyncEngine, runtime.sessionmaker.kw["bind"]),
                settings.observability.health_interval_s,
                lambda: (bot.turn_queue.in_flight(), bot.turn_queue.in_flight_max()),
            ):
                await start_when_leased(
                    bot,
                    settings.discord.bot_token.get_secret_value(),
                    stop=stop,
                    started=started,
                )
        finally:
            owner_drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await owner_drain
            health_server.close()
            await health_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
