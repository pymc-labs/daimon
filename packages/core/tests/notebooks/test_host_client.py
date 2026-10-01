"""Tests for daimon.core.notebooks.host_client.

Transport-level mocking via httpx.MockTransport — no AsyncMock on httpx methods.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

_HOST_URL = HttpUrl("http://notebook-host:8001")
_ADMIN_SECRET = SecretStr("supersecret")
_SLUG = "abc123"
_SOURCE = "import marimo as mo\napp = mo.App()"


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_publish_blog_to_host_puts_to_blogs_path_with_bearer() -> None:
    """publish_blog_to_host PUTs to /admin/blogs/{slug} with the bearer + source."""
    from daimon.core.notebooks.host_client import publish_blog_to_host

    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["method"] = req.method
        captured["auth"] = req.headers.get("Authorization", "")
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"url": "http://h/n/abc123/", "slug": _SLUG})

    async with _make_client(handler) as client:
        result = await publish_blog_to_host(
            slug=_SLUG,
            source=_SOURCE,
            host_url=_HOST_URL,
            admin_secret=_ADMIN_SECRET,
            client=client,
        )

    assert captured["url"] == "http://notebook-host:8001/admin/blogs/abc123", (
        "publish_blog_to_host must PUT to {host}/admin/blogs/{slug}"
    )
    assert captured["method"] == "PUT"
    assert captured["auth"] == "Bearer supersecret"
    assert result["slug"] == _SLUG


async def test_publish_blog_to_host_422_raises_validation_error() -> None:
    from daimon.core.notebooks.host_client import NotebookValidationError, publish_blog_to_host

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"detail": {"message": "x", "cell_errors": ["BoomError: nope"]}}
        )

    async with _make_client(handler) as client:
        with pytest.raises(NotebookValidationError) as excinfo:
            await publish_blog_to_host(
                slug=_SLUG,
                source=_SOURCE,
                host_url=_HOST_URL,
                admin_secret=_ADMIN_SECRET,
                client=client,
            )
    assert excinfo.value.cell_errors == ["BoomError: nope"]


async def test_delete_blog_from_host_deletes_and_tolerates_404() -> None:
    from daimon.core.notebooks.host_client import delete_blog_from_host

    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url)))
        return httpx.Response(404, text="gone")  # already absent — must not raise

    async with _make_client(handler) as client:
        await delete_blog_from_host(
            slug=_SLUG, host_url=_HOST_URL, admin_secret=_ADMIN_SECRET, client=client
        )
    assert seen == [("DELETE", "http://notebook-host:8001/admin/blogs/abc123")], (
        "delete must DELETE /admin/blogs/{slug}; a 404 is a successful no-op"
    )


async def test_delete_blog_from_host_raises_on_500() -> None:
    from daimon.core.notebooks.host_client import NotebookHostError, delete_blog_from_host

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _make_client(handler) as client:
        with pytest.raises(NotebookHostError, match="500"):
            await delete_blog_from_host(
                slug=_SLUG, host_url=_HOST_URL, admin_secret=_ADMIN_SECRET, client=client
            )


async def test_list_blogs_from_host_returns_body() -> None:
    from daimon.core.notebooks.host_client import list_blogs_from_host

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert str(req.url) == "http://notebook-host:8001/admin/blogs"
        return httpx.Response(200, json={"blogs": [{"slug": "pre-a", "alive": True}]})

    async with _make_client(handler) as client:
        body = await list_blogs_from_host(
            host_url=_HOST_URL, admin_secret=_ADMIN_SECRET, client=client
        )
    assert body["blogs"] == [{"slug": "pre-a", "alive": True}]


async def test_delete_notebook_from_host_returns_the_hosts_deleted_flag() -> None:
    from daimon.core.notebooks.host_client import delete_notebook_from_host

    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url)))
        return httpx.Response(200, json={"slug": _SLUG, "deleted": True})

    async with _make_client(handler) as client:
        deleted = await delete_notebook_from_host(
            slug=_SLUG, host_url=_HOST_URL, admin_secret=_ADMIN_SECRET, client=client
        )
    assert seen == [("DELETE", "http://notebook-host:8001/admin/notebooks/abc123")], (
        "one route deletes either kind"
    )
    assert deleted is True, "the host's answer is passed through, not assumed"


async def test_delete_notebook_from_host_reports_false_for_a_slug_that_was_not_there() -> None:
    from daimon.core.notebooks.host_client import delete_notebook_from_host

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"slug": _SLUG, "deleted": False})

    async with _make_client(handler) as client:
        deleted = await delete_notebook_from_host(
            slug=_SLUG, host_url=_HOST_URL, admin_secret=_ADMIN_SECRET, client=client
        )
    assert deleted is False, "a no-op delete must not read as a removal"


async def test_delete_notebook_from_host_treats_404_as_nothing_deleted() -> None:
    from daimon.core.notebooks.host_client import delete_notebook_from_host

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="gone")

    async with _make_client(handler) as client:
        deleted = await delete_notebook_from_host(
            slug=_SLUG, host_url=_HOST_URL, admin_secret=_ADMIN_SECRET, client=client
        )
    assert deleted is False, "an absent slug is not an error, but it is not a deletion either"


async def test_delete_notebook_from_host_raises_on_500() -> None:
    from daimon.core.notebooks.host_client import NotebookHostError, delete_notebook_from_host

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with _make_client(handler) as client:
        with pytest.raises(NotebookHostError, match="500"):
            await delete_notebook_from_host(
                slug=_SLUG, host_url=_HOST_URL, admin_secret=_ADMIN_SECRET, client=client
            )


async def test_list_notebooks_from_host_returns_body() -> None:
    from daimon.core.notebooks.host_client import list_notebooks_from_host

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert str(req.url) == "http://notebook-host:8001/admin/notebooks"
        return httpx.Response(200, json={"notebooks": [{"slug": "pre-a", "alive": True}]})

    async with _make_client(handler) as client:
        body = await list_notebooks_from_host(
            host_url=_HOST_URL, admin_secret=_ADMIN_SECRET, client=client
        )
    assert body["notebooks"] == [{"slug": "pre-a", "alive": True}]
