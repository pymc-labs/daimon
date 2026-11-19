"""Whether a budget's alerts can post to a channel, as the CLI and MCP check it."""

from __future__ import annotations

import httpx
from daimon.core.channel_post_check import alert_channel_problem
from daimon.core.config import DiscordSettings, Settings
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _settings(*, discord: bool) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        anthropic={"api_key": "sk-test"},  # pyright: ignore[reportArgumentType]
        discord=DiscordSettings(bot_token=SecretStr("token")) if discord else None,
    )


async def _problem(platform: str, *, discord: bool = True) -> str | None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users/@me"):
            return httpx.Response(200, json={"id": "B1"})
        return httpx.Response(404, json={"message": "Unknown Channel"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        return await alert_channel_problem(
            http,
            platform=platform,
            workspace_id="G1",
            channel_id="C1",
            label="#alerts",
            settings=_settings(discord=discord),
            sessionmaker=async_sessionmaker(class_=AsyncSession),
            fernet=None,
        )


async def test_alert_channel_when_discord_says_unknown_then_refused() -> None:
    problem = await _problem("discord")
    assert problem is not None and "#alerts" in problem, "a definite answer names the fix"


async def test_alert_channel_when_the_check_cannot_run_then_it_saves() -> None:
    assert await _problem("discord", discord=False) is None, "no bot token: not a refusal"


async def test_alert_channel_on_teams_then_checked_when_the_alert_posts() -> None:
    """Deliberate gap: the CLI and MCP have no team pick, so Teams checks at post time."""
    assert await _problem("teams") is None, "no Teams answer from the CLI or MCP"
