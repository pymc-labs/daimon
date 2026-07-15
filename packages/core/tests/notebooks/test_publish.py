"""Tests for daimon.core.notebooks.publish.

Transport-level mocking via httpx.MockTransport per project testing skill.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from daimon.core.config import NotebookSettings
from daimon.core.notebooks.publish import (
    _principal_prefix,  # pyright: ignore[reportPrivateUsage]
    delete_blog,
    list_blogs,
)
from pydantic import HttpUrl, SecretStr

pytestmark = pytest.mark.asyncio


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_delete_blog_resolves_prefix_and_calls_host() -> None:
    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"), admin_secret=SecretStr("secret")
    )
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(204)

    async with _make_client(handler) as client:
        await delete_blog(
            slug="radar-plots",
            notebook_settings=settings,
            client=client,
            principal_key="acct-1",
        )
    assert seen[0][0] == "DELETE"
    assert seen[0][1].startswith("/admin/blogs/") and seen[0][1].endswith("-radar-plots"), (
        "delete must target the principal-prefixed blog slug"
    )


async def test_list_blogs_filters_to_caller_prefix() -> None:
    """list_blogs returns only blogs whose slug carries the caller's principal prefix."""
    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"), admin_secret=SecretStr("secret")
    )
    mine = f"{_principal_prefix('acct-1')}-radar"
    theirs = f"{_principal_prefix('acct-2')}-radar"

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"blogs": [{"slug": mine, "alive": True}, {"slug": theirs, "alive": True}]},
        )

    async with _make_client(handler) as client:
        result = await list_blogs(notebook_settings=settings, client=client, principal_key="acct-1")

    slugs = [b["slug"] for b in result]
    assert slugs == [mine], "list_blogs must return only the caller's own blogs"
