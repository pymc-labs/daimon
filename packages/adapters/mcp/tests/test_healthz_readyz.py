"""Health routes are unauthenticated by construction."""

from __future__ import annotations

import httpx
import pytest
from daimon.adapters.mcp.server import create_mcp_app
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


async def test_healthz_returns_200_without_auth(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/healthz")
    assert r.status_code == 200, "healthz should return 200 without auth"
    assert r.text == "ok", "healthz body should be 'ok'"


async def test_readyz_returns_200_without_auth_when_db_reachable(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    app = create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        sessionmaker=sessionmaker,
        auth=StaticTokenVerifier(tokens={}),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/readyz")
    assert r.status_code == 200, "readyz should return 200 when DB is reachable"
    assert r.text == "ready", "readyz body should be 'ready'"
