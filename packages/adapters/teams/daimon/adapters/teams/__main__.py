"""``python -m daimon.adapters.teams`` entrypoint.

Boots the FastAPI/Teams SDK service: settings gate → logging → Sentry →
runtime → uvicorn on ``settings.teams.port``. SIGINT/SIGTERM land on
uvicorn's handler, which runs the lifespan shutdown — in-flight turns get a
bounded drain, then the SDK app stops. Mirrors slack/__main__.py's shape.
"""

from __future__ import annotations

import asyncio
import sys

import structlog
import uvicorn
from daimon.adapters.teams.http_service import create_teams_http_service
from daimon.adapters.teams.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.logging_setup import configure_log_level
from daimon.core.observability import init_sentry
from sentry_sdk.integrations.asyncio import AsyncioIntegration

log = structlog.get_logger()


async def main() -> None:
    settings = load_settings()
    if settings.teams is None:
        log.info("teams adapter disabled", reason="no teams settings")
        sys.exit(0)
    if not settings.crypto.keys:
        log.error("teams adapter requires DAIMON_CRYPTO__KEYS for token decryption")
        sys.exit(1)
    # Configure the JSON log chain BEFORE the first log line so it takes effect.
    configure_log_level(settings.log.level)
    init_sentry(
        dsn=settings.sentry.dsn.get_secret_value() if settings.sentry.dsn else None,
        environment=settings.sentry.environment,
        process="teams",
        release=None,
        traces_sample_rate=settings.sentry.traces_sample_rate,
        integrations=[AsyncioIntegration()],
    )
    async with build_runtime(settings) as runtime:
        service = create_teams_http_service(settings=settings.teams, runtime=runtime)
        server = uvicorn.Server(
            uvicorn.Config(
                service.app,
                host="0.0.0.0",
                port=settings.teams.port,
                log_config=None,  # JSON chain already configured; uvicorn uses structlog.
            )
        )
        log.info("starting_teams_adapter", port=settings.teams.port)
        await server.serve()


def run() -> None:
    """Console-script entry point (``daimon-teams``)."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
