"""Builder for the Discord runtime that Discord tests hand to the bot and its handlers."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from anthropic import AsyncAnthropic
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.testing.turn_fakes import deps_for
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def discord_runtime(
    *,
    settings: Settings,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    **fields: Any,  # noqa: ANN401 - forwards DiscordRuntime's fields
) -> DiscordRuntime:
    """A `DiscordRuntime` with no billing, an open notebook limiter and the stock turn deps."""
    defaults: dict[str, Any] = {
        "notebook_rate_limiter": RateLimiter(max_requests=999),
        "billing_config": None,
        "deployment_default": DeploymentDefault(),
        "resolver_cache": new_resolver_cache(),
        "turn_deps": replace(deps_for(), turn_path=None),
    }
    return DiscordRuntime(
        settings=settings, anthropic=anthropic, sessionmaker=sessionmaker, **(defaults | fields)
    )
