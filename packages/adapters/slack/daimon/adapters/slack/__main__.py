"""``python -m daimon.adapters.slack`` entrypoint.

Boots the Socket Mode listener: settings gate → logging → Sentry → runtime →
SocketModeClient + listener registration → SIGTERM drain handler → 8083 liveness
responder → event loop.  Mirrors discord/__main__.py exactly.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import structlog
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.health import start_liveness_responder
from daimon.core.logging_setup import configure_log_level
from daimon.core.observability import init_sentry
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()


async def _shutdown(app: SlackApp, client: SocketModeClient, stop: asyncio.Event) -> None:
    """Graceful shutdown: drain in-flight turns then signal main() to exit."""
    await app.drain_and_close(client)
    stop.set()


async def main() -> None:
    settings = load_settings()
    if settings.slack is None:
        log.info("slack adapter disabled", reason="no slack settings")
        sys.exit(0)
    if not settings.crypto.keys:
        log.error("slack adapter requires DAIMON_CRYPTO__KEYS for token decryption")
        sys.exit(1)
    # Configure the JSON log chain BEFORE the first log line so it takes effect.
    configure_log_level(settings.log.level)
    init_sentry(
        dsn=settings.sentry.dsn.get_secret_value() if settings.sentry.dsn else None,
        environment=settings.sentry.environment,
        process="slack",
        release=None,
        traces_sample_rate=settings.sentry.traces_sample_rate,
        integrations=[AsyncioIntegration()],
    )
    async with build_runtime(settings) as runtime:
        app = SlackApp(runtime=runtime)
        client = SocketModeClient(
            app_token=settings.slack.app_token.get_secret_value(),
            web_client=AsyncWebClient(),
        )
        client.socket_mode_request_listeners.append(app.on_request)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk list attr

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        # Keep strong references to drain tasks — asyncio holds weak refs only.
        _bg_tasks: set[asyncio.Task[None]] = set()

        def _on_signal() -> None:
            if app.draining:  # re-entry guard: second signal is a no-op
                return
            task: asyncio.Task[None] = asyncio.create_task(_shutdown(app, client, stop))
            _bg_tasks.add(task)
            task.add_done_callback(_bg_tasks.discard)

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _on_signal)

        # 8083 liveness responder co-located on the asyncio loop so a hung
        # loop fails the probe and Fly restarts the process.
        health_server = await start_liveness_responder(settings.slack.health_port)
        log.info("starting_slack_listener", health_port=settings.slack.health_port)
        try:
            await client.connect()
            await stop.wait()  # released by _shutdown after drain completes
        finally:
            health_server.close()
            await health_server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
