from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.mcp import server
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.errors import DatabaseNotMigratedError
from daimon.testing.ma import build_stub_anthropic
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from starlette.types import ASGIApp, Message


def _settings() -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(
            jwt_secret=SecretStr("a" * 32),
            public_url=HttpUrl("https://x/mcp"),
        ),
    )


def _app(sessionmaker: object) -> ASGIApp:
    return server.create_mcp_app(
        settings=_settings(),
        sessionmaker=sessionmaker,  # type: ignore[arg-type]
        auth=StaticTokenVerifier(tokens={}),
        anthropic=build_stub_anthropic(),
    )


@asynccontextmanager
async def _lifespan(app: ASGIApp) -> AsyncIterator[None]:
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()
    send_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        await send_queue.put(message)

    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
    await receive_queue.put({"type": "lifespan.startup"})
    startup = await send_queue.get()
    assert startup["type"] == "lifespan.startup.complete", startup
    try:
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        shutdown = await send_queue.get()
        assert shutdown["type"] == "lifespan.shutdown.complete", shutdown
        await task


@pytest.mark.asyncio
async def test_mcp_lifespan_runs_database_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    check = AsyncMock()
    monkeypatch.setattr(server, "ensure_database_migrated", check)
    sessionmaker = MagicMock()

    async with _lifespan(_app(sessionmaker)):
        pass

    check.assert_awaited_once_with(sessionmaker)


@pytest.mark.asyncio
async def test_mcp_lifespan_surfaces_the_migration_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = AsyncMock(
        side_effect=DatabaseNotMigratedError(
            "database not migrated.\n  run: uv run alembic upgrade head"
        )
    )
    monkeypatch.setattr(server, "ensure_database_migrated", check)
    app = _app(MagicMock())
    receive_queue: asyncio.Queue[Message] = asyncio.Queue()
    send_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await receive_queue.get()

    async def send(message: Message) -> None:
        await send_queue.put(message)

    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
    await receive_queue.put({"type": "lifespan.startup"})
    startup = await send_queue.get()

    assert startup["type"] == "lifespan.startup.failed", startup
    assert "uv run alembic upgrade head" in startup["message"]
    with contextlib.suppress(DatabaseNotMigratedError):
        await task


@pytest.mark.asyncio
async def test_readyz_uses_the_schema_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    check = AsyncMock()
    monkeypatch.setattr(server, "ensure_database_migrated", check)
    sessionmaker = MagicMock()
    app = _app(sessionmaker)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.text == "ready"
    check.assert_awaited_once_with(sessionmaker)
