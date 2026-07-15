"""SlackRuntime -- DI bundle for the Slack adapter process."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from anthropic import AsyncAnthropic
from daimon.core.config import Settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.scope import DeploymentDefault
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@dataclass(frozen=True)
class SlackRuntime:
    settings: Settings
    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    http_client: httpx.AsyncClient
    # Bottom tier of the channel→tenant→deployment config cascade. Defaults to
    # empty (no deployment fallback) so existing construction sites stay valid;
    # build_runtime always parses the real defaults/config.yaml.
    deployment_default: DeploymentDefault = field(default_factory=DeploymentDefault)


@asynccontextmanager
async def build_runtime(settings: Settings) -> AsyncIterator[SlackRuntime]:
    engine = build_engine(str(settings.database.url))
    sm = build_session_factory(engine)
    deployment_default = parse_deployment_default(settings.defaults_root)
    async with (
        AsyncAnthropic(
            api_key=settings.anthropic.api_key.get_secret_value(),
            base_url=str(settings.anthropic.base_url),
        ) as anthropic,
        httpx.AsyncClient(timeout=30.0) as http_client,
    ):
        try:
            yield SlackRuntime(
                settings=settings,
                anthropic=anthropic,
                sessionmaker=sm,
                http_client=http_client,
                deployment_default=deployment_default,
            )
        finally:
            await engine.dispose()
