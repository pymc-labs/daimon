"""Tests for the blog MCP tools (delete_blog / list_blogs).

Transport-injected httpx.MockTransport per project testing skill.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast
from unittest.mock import create_autospec

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.notebook import (
    _delete_blog_impl,  # pyright: ignore[reportPrivateUsage]
    _list_blogs_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, NotebookSettings, Settings
from daimon.core.notebooks.publish import _principal_prefix  # pyright: ignore[reportPrivateUsage]
from daimon.core.scope import DeploymentDefault
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _make_settings(*, host_url: str | None = None, admin_secret: str | None = None) -> Settings:
    return Settings(
        database=DatabaseSettings(
            url=PostgresDsn("postgresql+asyncpg://daimon:daimon@localhost:5432/daimon")
        ),
        anthropic=AnthropicSettings(api_key=SecretStr("test-key")),
        notebook=NotebookSettings(
            host_url=HttpUrl(host_url) if host_url else None,
            admin_secret=SecretStr(admin_secret) if admin_secret else None,
        ),
        _env_file=None,  # type: ignore[call-arg]
    )


def _make_runtime(settings: Settings) -> McpRuntime:
    fake_sessionmaker: async_sessionmaker[AsyncSession] = create_autospec(
        async_sessionmaker, instance=True
    )
    return McpRuntime(
        session_factory=fake_sessionmaker,
        client=AsyncAnthropic(api_key="test-key"),
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> type[httpx.AsyncClient]:
    class _FakeClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    return _FakeClient  # type: ignore[return-value]


async def test_delete_blog_impl_calls_host_delete() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://h:8001", admin_secret="bearer"))
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(204)

    await _delete_blog_impl(
        runtime, principal_key="acct-1", slug="radar", client_factory=_factory(handler)
    )
    assert seen[0][0] == "DELETE" and seen[0][1].endswith("-radar")


async def test_list_blogs_impl_returns_only_callers_blogs() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://h:8001", admin_secret="bearer"))
    mine = f"{_principal_prefix('acct-1')}-radar"
    theirs = f"{_principal_prefix('acct-2')}-radar"

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"blogs": [{"slug": mine}, {"slug": theirs}]})

    result = await _list_blogs_impl(
        runtime, principal_key="acct-1", client_factory=_factory(handler)
    )
    blogs = result["blogs"]
    assert isinstance(blogs, list), "blogs must be a list"
    blogs_list = cast("list[object]", blogs)
    assert len(blogs_list) == 1, "must filter to the caller's blogs"
    first = blogs_list[0]
    assert isinstance(first, dict) and first["slug"] == mine, "the one blog must be the caller's"
