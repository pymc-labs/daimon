"""Capability tokens and OAuth query values stay out of the server's logs.

Runs the real uvicorn server with its default (deployment) logging config on
an ephemeral loopback port, against the production MCP app factory and the
production Slack file proxy, and reads what uvicorn actually wrote.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.server import create_mcp_app
from daimon.adapters.mcp.slack_file_proxy import build_slack_file_proxy_route
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.observability import install_log_redaction
from daimon.core.slack_file_token import mint_file_token
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.applications import Starlette
from starlette.routing import Route

_FILE_SECRET = "test-signing-key"


@contextmanager
def _serve(factory: Callable[[], object]) -> Iterator[str]:
    """Run uvicorn the way deployment does (factory, default log config)."""
    config = uvicorn.Config(factory, factory=True, host="127.0.0.1", port=0, lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


def _production_mcp_app() -> object:
    return create_mcp_app(
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(
                jwt_secret=SecretStr("x" * 32), public_url=HttpUrl("https://x.example/mcp")
            ),
            crypto=CryptoSettings(keys=(SecretStr(Fernet.generate_key().decode()),)),
        ),
        # A database nothing listens on: every session fails to connect.
        sessionmaker=async_sessionmaker(
            create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/d"), expire_on_commit=False
        ),
        auth=StaticTokenVerifier(tokens={}),
    )


def test_production_mcp_app_logs_no_upload_token_or_oauth_query_values(
    capfd: pytest.CaptureFixture[str],
) -> None:
    upload_token = secrets.token_urlsafe(24)
    code, state = secrets.token_urlsafe(16), secrets.token_urlsafe(16)

    with _serve(_production_mcp_app) as base:
        try:
            status = httpx.put(f"{base}/uploads/{upload_token}", content=b"data").status_code
        except httpx.HTTPError:
            status = 500  # the server dropped the connection after the handler failed
        for path in ("/oauth/mcp/callback", "/oauth/slack/callback"):
            with contextlib.suppress(httpx.HTTPError):
                httpx.get(f"{base}{path}", params={"code": code, "state": state}, timeout=20)

    out = capfd.readouterr()
    logged = out.out + out.err
    assert status >= 500
    assert "/uploads/" in logged, "the access log line was written"
    for canary in (upload_token, code, state):
        assert canary not in logged


def test_slack_file_proxy_success_logs_no_file_token(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    token = mint_file_token(
        team_id="T1", file_id="F1", exp=int(time.time()) + 600, secret=_FILE_SECRET
    )

    @asynccontextmanager
    async def _database() -> AsyncIterator[object]:
        yield object()

    async def _bot_token(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(encrypted_token=fernet.encrypt(b"bot-token"))

    async def _fetch(*args: object) -> tuple[bytes, str, str]:
        return b"file", "text/plain", "f.txt"

    monkeypatch.setattr("daimon.adapters.mcp.slack_file_proxy.get_slack_bot_token", _bot_token)

    def _factory() -> object:
        # The same call create_mcp_app makes once uvicorn has set up logging.
        install_log_redaction()
        handler = build_slack_file_proxy_route(
            sessionmaker=_database,  # pyright: ignore[reportArgumentType]
            fernet=fernet,
            secret=_FILE_SECRET,
            fetch_file=_fetch,  # pyright: ignore[reportArgumentType]
            now=time.time,
        )
        return Starlette(routes=[Route("/slack/file/{token}", handler)])

    with _serve(_factory) as base, httpx.Client(base_url=base, timeout=20) as client:
        response = client.get(f"/slack/file/{token}")

    out = capfd.readouterr()
    logged = out.out + out.err
    assert response.status_code == 200
    assert "/slack/file/" in logged, "the access log line was written"
    assert token not in logged
    for part in token.split("."):
        assert part not in logged


def test_failing_tool_traceback_in_the_real_app_is_redacted_and_has_no_locals(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """fastmcp logs tool exceptions through its own rich handler; that output is redacted."""
    import json

    from fastmcp import Client

    text_canary, local_canary = secrets.token_hex(12), secrets.token_hex(12)
    from fastmcp import FastMCP

    # create_mcp_app installs the process-wide log redaction; a bare server on
    # the same fastmcp loggers exercises fastmcp's own tool-error logging
    # without the app's auth and middleware in the way.
    _production_mcp_app()
    mcp = FastMCP("redaction-probe")

    @mcp.tool
    def explode() -> str:
        held_secret = local_canary  # noqa: F841 - a frame local that must not print
        raise RuntimeError(json.dumps({"api_key": text_canary}))

    async def _call() -> None:
        async with Client(mcp) as client:
            with contextlib.suppress(Exception):
                await client.call_tool("explode", {})

    asyncio.run(_call())

    out = capfd.readouterr()
    logged = out.out + out.err
    assert "RuntimeError" in logged, "the tool failure was logged"
    assert text_canary not in logged
    assert local_canary not in logged


def test_structlog_after_create_mcp_app_renders_redacted_json_without_locals(
    capfd: pytest.CaptureFixture[str],
) -> None:
    import json

    import structlog

    text_canary, local_canary = secrets.token_hex(12), secrets.token_hex(12)
    _production_mcp_app()
    capfd.readouterr()

    held_secret = local_canary  # noqa: F841 - a frame local that must not print
    try:
        raise RuntimeError(json.dumps({"client_secret": text_canary}))
    except RuntimeError:
        structlog.get_logger("daimon.test").exception("tool.failed", api_token=text_canary)

    logged = capfd.readouterr().out
    line = next(line for line in logged.splitlines() if "tool.failed" in line)
    assert json.loads(line)["event"] == "tool.failed", "rendered by the JSON chain"
    assert text_canary not in logged
    assert local_canary not in logged
