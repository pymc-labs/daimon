"""``python -m daimon.adapters.web``: settings gate, logging, Sentry, runtime, uvicorn."""

from __future__ import annotations

import asyncio
import sys
from types import FrameType

import structlog
import uvicorn
from daimon.adapters.web.app import create_app
from daimon.adapters.web.boot_sweep import (
    sweep_expired_web_sessions_forever,
    sweep_orphaned_web_turns,
    sweep_stale_web_uploads_forever,
)
from daimon.adapters.web.owner_link import owner_link
from daimon.adapters.web.runtime import build_runtime
from daimon.adapters.web.single_process import WebProcessAlreadyRunning
from daimon.adapters.web.turns import WebTurns
from daimon.core.config import Settings, WebSettings, load_settings
from daimon.core.logging_setup import configure_logging
from daimon.core.observability import init_sentry
from daimon.core.runtime_health import log_runtime_started
from sentry_sdk.integrations.asyncio import AsyncioIntegration

log = structlog.get_logger()

#: On shutdown, running turns get this long to finish before they are stopped.
DRAIN_S = 60.0


class _Server(uvicorn.Server):
    """Stops admitting turns and drains them on the first exit signal, so the open
    streams end and the server can shut down."""

    def __init__(self, config: uvicorn.Config, turns: WebTurns | None) -> None:
        super().__init__(config)
        self.turns = turns
        self.drained: asyncio.Task[None] | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self.turns is not None and self.drained is None:
            self.drained = asyncio.get_running_loop().create_task(self.turns.drain(DRAIN_S))
        super().handle_exit(sig, frame)


async def main() -> None:
    settings = load_settings()
    configure_logging(settings.log.level)
    web = settings.web
    if web is None:
        log.info("web adapter disabled", reason="no web settings")
        sys.exit(0)
    if not settings.crypto.keys:
        log.error("web adapter requires DAIMON_CRYPTO__KEYS for token encryption")
        sys.exit(1)
    if not web.dev and not settings.hub.discord_configured:
        log.error(
            "web.signin.unconfigured",
            hint="set DAIMON_HUB__DISCORD_CLIENT_ID and DAIMON_HUB__DISCORD_CLIENT_SECRET",
        )
        sys.exit(1)
    init_sentry(
        dsn=settings.sentry.dsn.get_secret_value() if settings.sentry.dsn else None,
        environment=settings.sentry.environment,
        process="web",
        release=None,
        traces_sample_rate=settings.sentry.traces_sample_rate,
        integrations=[AsyncioIntegration()],
    )
    try:
        await _serve(settings, web)
    except WebProcessAlreadyRunning:
        log.error(
            "web.process.already_running",
            hint="one web process per deployment: stop the running one before starting another",
        )
        sys.exit(1)


async def _serve(settings: Settings, web: WebSettings) -> None:
    async with build_runtime(settings) as runtime:
        if runtime.turns is not None:
            await sweep_orphaned_web_turns(runtime.turns.deps.anthropic, runtime.sessionmaker)
        server = _Server(
            uvicorn.Config(
                create_app(runtime),
                host="0.0.0.0",
                port=web.port,
                log_config=None,  # JSON chain already configured; uvicorn uses structlog.
                timeout_graceful_shutdown=int(DRAIN_S) + 20,
            ),
            runtime.turns,
        )
        log_runtime_started("web")
        log.info("starting_web_adapter", port=web.port)
        sweepers = [
            asyncio.create_task(sweep_expired_web_sessions_forever(runtime.sessionmaker)),
            asyncio.create_task(sweep_stale_web_uploads_forever(runtime.sessionmaker)),
        ]
        try:
            await server.serve()
        finally:
            for sweeper in sweepers:
                sweeper.cancel()
        if runtime.turns is not None:
            await (server.drained or runtime.turns.drain(0))


def run() -> None:
    """Console-script entry point: ``daimon-web``, or ``daimon-web owner-link``."""
    if sys.argv[1:2] == ["owner-link"]:
        print(asyncio.run(owner_link(load_settings(), sys.argv[2:])))
        return
    asyncio.run(main())


if __name__ == "__main__":
    run()
