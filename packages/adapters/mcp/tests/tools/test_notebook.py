"""Tests for the notebook MCP tools: upload-URL minters, list, and delete."""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import create_autospec

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.notebook import (
    _create_attachment_upload_impl,  # pyright: ignore[reportPrivateUsage]
    _create_notebook_upload_impl,  # pyright: ignore[reportPrivateUsage]
    _delete_notebook_impl,  # pyright: ignore[reportPrivateUsage]
    _list_notebooks_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    NotebookSettings,
    Settings,
)
from daimon.core.notebooks.publish import _principal_prefix  # pyright: ignore[reportPrivateUsage]
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AUTH = AuthIdentity(account_id=uuid.UUID(int=1), tenant_id=uuid.UUID(int=2), role=Role.USER)


@pytest.fixture(autouse=True)
def _open_policy_for_minter_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load_open_policy(runtime: McpRuntime, auth: AuthIdentity) -> TenantAccessPolicy:
        return OPEN_ACCESS_POLICY

    monkeypatch.setattr(
        "daimon.adapters.mcp.tools._channel_policy.load_channel_policy", load_open_policy
    )


def _make_settings(
    *,
    host_url: str | None = None,
    admin_secret: str | None = None,
    allow_editable: bool = False,
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
            allow_editable=allow_editable,
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


def _factory(handler: Callable[[httpx.Request], httpx.Response]) -> type[httpx.AsyncClient]:
    class _FakeClient(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    return _FakeClient  # type: ignore[return-value]


async def test_create_notebook_upload_impl_permanent_mints_blog_op() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    out = await _create_notebook_upload_impl(
        runtime, auth=_AUTH, slug="radar", permanent=True, principal_key="acct-1"
    )
    assert out["upload_url"].startswith("http://nb:8001/upload/"), "returns a host upload URL"
    assert _token_payload(out["upload_url"])["op"] == "blog", (
        "permanent=True must mint the run-mode blog op, not a scratch notebook"
    )


async def test_create_notebook_upload_impl_random_slug() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    out = await _create_notebook_upload_impl(
        runtime, auth=_AUTH, slug=None, permanent=False, principal_key="acct-1"
    )
    assert _token_payload(out["upload_url"])["op"] == "notebook", "token op is notebook"


async def test_create_attachment_upload_impl_signs_name() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    out = await _create_attachment_upload_impl(
        runtime, auth=_AUTH, slug="radar", name="d.nc", principal_key="acct-1"
    )
    assert _token_payload(out["upload_url"])["name"] == "d.nc", "attachment name signed into token"


async def test_create_notebook_upload_impl_raises_when_host_unset() -> None:
    runtime = _make_runtime(_make_settings(host_url=None, admin_secret=None))
    with pytest.raises(ToolError, match="not configured"):
        await _create_notebook_upload_impl(
            runtime, auth=_AUTH, slug="x", permanent=True, principal_key="acct-1"
        )


async def test_create_attachment_upload_impl_rejects_bad_name() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    with pytest.raises(ToolError):
        await _create_attachment_upload_impl(
            runtime, auth=_AUTH, slug="x", name="../bad", principal_key="acct-1"
        )


async def test_delete_notebook_impl_namespaces_the_bare_slug_once() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://h:8001", admin_secret="bearer"))
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"slug": "x", "deleted": True})

    out = await _delete_notebook_impl(
        runtime, principal_key="acct-1", slug="radar", client_factory=_factory(handler)
    )
    prefix = _principal_prefix("acct-1")
    assert seen == [("DELETE", f"/admin/notebooks/{prefix}-radar")], (
        "the bare slug is namespaced exactly once, against the unified notebooks route"
    )
    assert out["deleted"] is True, "a real removal reports deleted=True"


async def test_delete_notebook_impl_reports_false_when_nothing_existed() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://h:8001", admin_secret="bearer"))

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"slug": "x", "deleted": False})

    out = await _delete_notebook_impl(
        runtime, principal_key="acct-1", slug="never-existed", client_factory=_factory(handler)
    )
    assert out["deleted"] is False, (
        "a delete that removed nothing must not read as success — that is what hid a "
        "delete which silently targeted a doubly-namespaced slug"
    )


async def test_list_notebooks_impl_returns_bare_slugs_that_round_trip_to_delete() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://h:8001", admin_secret="bearer"))
    prefix = _principal_prefix("acct-1")
    mine = f"{prefix}-radar"
    theirs = f"{_principal_prefix('acct-2')}-radar"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/admin/blogs"):
            return httpx.Response(200, json={"blogs": [{"slug": mine}, {"slug": theirs}]})
        return httpx.Response(200, json={"notebooks": [{"slug": mine}, {"slug": theirs}]})

    result = await _list_notebooks_impl(
        runtime, principal_key="acct-1", client_factory=_factory(handler)
    )
    notebooks = result["notebooks"]
    assert isinstance(notebooks, list), "notebooks must be a list"
    entries = cast("list[dict[str, object]]", notebooks)
    assert len(entries) == 1, "another principal's notebook must not be listed, and no duplicates"
    assert entries[0]["slug"] == "radar", (
        "the listed slug is the bare name delete_notebook accepts; returning the "
        "namespaced form makes the round-trip prefix it twice and hit nothing"
    )
    assert entries[0]["permanent"] is True, "a slug in the blog registry is permanent"


async def test_list_notebooks_impl_marks_scratch_notebooks_not_permanent() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://h:8001", admin_secret="bearer"))
    mine = f"{_principal_prefix('acct-1')}-scratch"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/admin/blogs"):
            return httpx.Response(200, json={"blogs": []})
        return httpx.Response(200, json={"notebooks": [{"slug": mine, "alive": True}]})

    result = await _list_notebooks_impl(
        runtime, principal_key="acct-1", client_factory=_factory(handler)
    )
    entries = cast("list[dict[str, object]]", result["notebooks"])
    assert entries[0]["permanent"] is False, "an unregistered live process is a scratch notebook"
    assert entries[0]["alive"] is True, "host fields are passed through untouched"


async def test_create_notebook_upload_impl_editable_mints_editor_op() -> None:
    runtime = _make_runtime(
        _make_settings(host_url="http://nb:8001", admin_secret="s", allow_editable=True)
    )
    out = await _create_notebook_upload_impl(
        runtime, auth=_AUTH, slug="x", permanent=False, editable=True, principal_key="acct-1"
    )
    assert _token_payload(out["upload_url"])["op"] == "notebook_edit"


async def test_create_notebook_upload_impl_refuses_editable_blog() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    with pytest.raises(ToolError, match="editable"):
        await _create_notebook_upload_impl(
            runtime, auth=_AUTH, slug="x", permanent=True, editable=True, principal_key="acct-1"
        )


async def test_create_notebook_upload_impl_refuses_editable_unless_operator_allows() -> None:
    runtime = _make_runtime(_make_settings(host_url="http://nb:8001", admin_secret="s"))
    with pytest.raises(ToolError, match="allow_editable"):
        await _create_notebook_upload_impl(
            runtime, auth=_AUTH, slug="x", permanent=False, editable=True, principal_key="acct-1"
        )
