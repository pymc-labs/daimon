"""Tests for notebook MCP upload-URL minter tools."""

from __future__ import annotations

import base64
import json
from unittest.mock import create_autospec

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.notebook import (
    _create_attachment_upload_impl,  # pyright: ignore[reportPrivateUsage]
    _create_blog_upload_impl,  # pyright: ignore[reportPrivateUsage]
    _create_notebook_upload_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    NotebookSettings,
    Settings,
)
from daimon.core.scope import DeploymentDefault
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


def _make_settings(
    *,
    host_url: str | None = None,
    admin_secret: str | None = None,
) -> Settings:
    """Build a minimal Settings with the given notebook sub-config."""
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
    """Build a minimal McpRuntime for tool tests — no real DB or Anthropic client."""
    fake_sessionmaker: async_sessionmaker[AsyncSession] = create_autospec(
        async_sessionmaker, instance=True
    )
    return McpRuntime(
        session_factory=fake_sessionmaker,
        client=AsyncAnthropic(api_key="test-key"),
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


def _token_payload(upload_url: str) -> dict[str, object]:
    token = upload_url.rsplit("/upload/", 1)[1]
    payload_b64 = token.split(".", 1)[0]
    raw = base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    return json.loads(raw)


async def test_create_blog_upload_impl_returns_signed_url() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    out = await _create_blog_upload_impl(runtime, slug="radar", principal_key="acct-1")
    assert out["upload_url"].startswith("http://nb:8001/upload/"), "returns a host upload URL"
    assert _token_payload(out["upload_url"])["op"] == "blog", "token op is blog"


async def test_create_notebook_upload_impl_random_slug() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    out = await _create_notebook_upload_impl(runtime, slug=None, principal_key="acct-1")
    assert _token_payload(out["upload_url"])["op"] == "notebook", "token op is notebook"


async def test_create_attachment_upload_impl_signs_name() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    out = await _create_attachment_upload_impl(
        runtime, slug="radar", name="d.nc", principal_key="acct-1"
    )
    assert _token_payload(out["upload_url"])["name"] == "d.nc", "attachment name signed into token"


async def test_create_blog_upload_impl_raises_when_host_unset() -> None:
    runtime = _make_runtime(_make_settings(host_url=None, admin_secret=None))
    with pytest.raises(ToolError, match="not configured"):
        await _create_blog_upload_impl(runtime, slug="x", principal_key="acct-1")


async def test_create_attachment_upload_impl_rejects_bad_name() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    with pytest.raises(ToolError):
        await _create_attachment_upload_impl(
            runtime, slug="x", name="../bad", principal_key="acct-1"
        )
