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
from daimon.adapters.discord.confirm_click import start_confirm_click_server
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

# Closing the gateway takes well under a second; past this the lease is left to
# drop with the owner connection, and the successor waits on liveness instead.
GATEWAY_CLOSE_WAIT_S = 30


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
    try:
        await bot.start(token)
    finally:
        # Here rather than in the drain task: bot.start returns once close()
        # is done, and the runtime then tears down the owner connection the
        # lease lives on. Released only after a clean close, so a successor
        # connects after this process has left the gateway; on ownership loss
        # the successor must wait for this process to stop being alive.
        if lease is not None and bot.is_closed() and await _gateway_closed(bot):
            await lease.release()


async def _gateway_closed(bot: DaimonBot) -> bool:
    """Whether the close that `is_closed()` reports has finished, and cleanly.

    `is_closed()` turns true when closing starts, and `start()` can return while
    the drain task is still inside `close()`. `close()` is idempotent and waits
    for that close to finish (or re-raises its failure).
    """
    try:
        async with asyncio.timeout(GATEWAY_CLOSE_WAIT_S):
            await bot.close()
    except Exception as err:  # includes TimeoutError
        log.warning("discord.gateway_close_unconfirmed", error_type=type(err).__name__)
        return False
    return not bot.runtime.ownership_lost.is_set()


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
        # Staging test infrastructure; None unless switched on on a staging host.
        confirm_click_server = await start_confirm_click_server(bot, settings)
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
            if confirm_click_server is not None:
                confirm_click_server.close()
                await confirm_click_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
