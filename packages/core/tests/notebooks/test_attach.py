"""Tests for daimon.core.notebooks.attach.

Transport-level mocking via httpx.MockTransport per project testing skill.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
import pytest
from daimon.core.config import NotebookSettings
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.notebooks.attach import (
    InvalidAttachmentError,
    attach_notebook_data,
)
from daimon.core.notebooks.host_client import NotebookHostError, attach_to_host
from daimon.core.notebooks.publish import (
    HostNotConfiguredError,
    NotebookRateLimitError,
)
from pydantic import HttpUrl, SecretStr

pytestmark = pytest.mark.asyncio


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _attach_ok_handler(req: httpx.Request) -> httpx.Response:
    # URL: /admin/notebooks/{slug}/data/{name}
    parts = req.url.path.rstrip("/").split("/")
    name = parts[-1]
    slug = parts[-3]
    return httpx.Response(
        200,
        json={
            "slug": slug,
            "name": name,
            "size_bytes": len(req.content),
            "path": f"data/{name}",
        },
    )


# ---------- attach_to_host (host_client.py) ----------


async def test_attach_to_host_puts_raw_bytes() -> None:
    """attach_to_host PUTs raw bytes with bearer + octet-stream content type."""
    captured: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["method"] = req.method
        captured["path"] = req.url.path
        captured["content"] = req.content
        captured["auth"] = req.headers.get("authorization")
        captured["content_type"] = req.headers.get("content-type")
        return httpx.Response(
            200,
            json={"slug": "abc", "name": "x.csv", "size_bytes": 5, "path": "data/x.csv"},
        )

    async with _make_client(handler) as client:
        body = await attach_to_host(
            slug="abc",
            name="x.csv",
            content=b"hello",
            host_url=HttpUrl("http://notebook-host:8001"),
            admin_secret=SecretStr("secret"),
            client=client,
        )

    assert captured["method"] == "PUT", "must use PUT"
    assert captured["path"] == "/admin/notebooks/abc/data/x.csv", (
        f"path must include /data/{{name}}, got {captured['path']!r}"
    )
    assert captured["content"] == b"hello", "body bytes must pass through unchanged"
    assert captured["auth"] == "Bearer secret", "must send bearer token"
    assert captured["content_type"] == "application/octet-stream", (
        "content type must be octet-stream"
    )
    assert body["slug"] == "abc"


async def test_attach_to_host_raises_on_non_2xx() -> None:
    """attach_to_host raises NotebookHostError on non-2xx with status in message."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(413, text="payload too large")

    async with _make_client(handler) as client:
        with pytest.raises(NotebookHostError, match="413"):
            await attach_to_host(
                slug="abc",
                name="x.csv",
                content=b"hello",
                host_url=HttpUrl("http://notebook-host:8001"),
                admin_secret=SecretStr("secret"),
                client=client,
            )


async def test_attach_to_host_strips_trailing_slash_on_host_url() -> None:
    """A trailing slash on host_url does not produce a double-slash in the path."""
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        return httpx.Response(
            200,
            json={"slug": "abc", "name": "x.csv", "size_bytes": 0, "path": "data/x.csv"},
        )

    async with _make_client(handler) as client:
        await attach_to_host(
            slug="abc",
            name="x.csv",
            content=b"",
            host_url=HttpUrl("http://notebook-host:8001/"),
            admin_secret=SecretStr("secret"),
            client=client,
        )

    assert "//admin" not in captured["url"], (
        f"trailing slash must be stripped, got {captured['url']!r}"
    )


# ---------- attach_notebook_data (attach.py) ----------


async def test_attach_raises_when_host_url_unset() -> None:
    """attach_notebook_data raises HostNotConfiguredError when host_url is None."""
    settings = NotebookSettings(host_url=None, admin_secret=None)

    def handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("host should not be called when not configured")

    async with _make_client(handler) as client:
        with pytest.raises(HostNotConfiguredError):
            await attach_notebook_data(
                content=b"hello",
                name="x.csv",
                notebook_settings=settings,
                client=client,
            )


async def test_attach_raises_when_admin_secret_unset() -> None:
    """attach_notebook_data raises HostNotConfiguredError when admin_secret is None."""
    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=None,
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("host should not be called when not configured")

    async with _make_client(handler) as client:
        with pytest.raises(HostNotConfiguredError):
            await attach_notebook_data(
                content=b"hello",
                name="x.csv",
                notebook_settings=settings,
                client=client,
            )


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "-leading",
        ".leading",
        "has space",
        "has/slash",
        "../etc",
        "x" * 65,
    ],
)
async def test_attach_rejects_unsafe_name(bad_name: str) -> None:
    """Invalid attachment names raise InvalidAttachmentError before touching the host."""
    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=SecretStr("secret"),
    )

    def reject_handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("host should not be called when name is invalid")

    async with _make_client(reject_handler) as client:
        with pytest.raises(InvalidAttachmentError):
            await attach_notebook_data(
                content=b"hello",
                name=bad_name,
                notebook_settings=settings,
                client=client,
                principal_key="acct-1",
            )


async def test_attach_rejects_oversize_against_daimon_setting() -> None:
    """Content larger than max_attachment_bytes raises InvalidAttachmentError."""
    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=SecretStr("secret"),
        max_attachment_bytes=10,
    )

    def reject_handler(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("host should not be called when content is oversize")

    async with _make_client(reject_handler) as client:
        with pytest.raises(InvalidAttachmentError, match="exceeds"):
            await attach_notebook_data(
                content=b"x" * 11,
                name="x.csv",
                notebook_settings=settings,
                client=client,
                principal_key="acct-1",
            )


async def test_attach_returns_size_and_path() -> None:
    """attach_notebook_data returns the host body with slug/name/size_bytes/path."""
    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=SecretStr("secret"),
    )

    async with _make_client(_attach_ok_handler) as client:
        result = await attach_notebook_data(
            content=b"hello",
            name="x.csv",
            notebook_settings=settings,
            client=client,
            principal_key="acct-1",
            slug="ws",
        )

    assert result["name"] == "x.csv", "name should pass through"
    assert result["size_bytes"] == 5, "size_bytes should reflect actual body length"
    assert result["path"] == "data/x.csv", "path should be data/<name>"
    assert isinstance(result["slug"], str)
    assert result["slug"].endswith("-ws"), (
        f"resolved slug should end with -ws, got: {result['slug']!r}"
    )


async def test_attach_namespaces_by_principal() -> None:
    """Two principals passing the same slug get different URL paths."""
    captured_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_paths.append(req.url.path)
        parts = req.url.path.rstrip("/").split("/")
        name = parts[-1]
        slug = parts[-3]
        return httpx.Response(
            200,
            json={
                "slug": slug,
                "name": name,
                "size_bytes": len(req.content),
                "path": f"data/{name}",
            },
        )

    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=SecretStr("secret"),
    )

    async with _make_client(handler) as client:
        result_a = await attach_notebook_data(
            content=b"hello",
            name="x.csv",
            notebook_settings=settings,
            client=client,
            principal_key="acct-1",
            slug="ws",
        )
        result_b = await attach_notebook_data(
            content=b"hello",
            name="x.csv",
            notebook_settings=settings,
            client=client,
            principal_key="acct-2",
            slug="ws",
        )

    assert result_a["slug"] != result_b["slug"], (
        "same agent slug from different principals must namespace to different URL slugs"
    )
    assert captured_paths[0] != captured_paths[1], "host URL path must differ between principals"


async def test_attach_mints_random_slug_when_slug_none() -> None:
    """slug=None mints a fresh 22-char random slug via _resolve_slug."""
    captured_paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured_paths.append(req.url.path)
        parts = req.url.path.rstrip("/").split("/")
        name = parts[-1]
        slug = parts[-3]
        return httpx.Response(
            200,
            json={
                "slug": slug,
                "name": name,
                "size_bytes": len(req.content),
                "path": f"data/{name}",
            },
        )

    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=SecretStr("secret"),
    )

    async with _make_client(handler) as client:
        result = await attach_notebook_data(
            content=b"hello",
            name="x.csv",
            notebook_settings=settings,
            client=client,
        )

    assert isinstance(result["slug"], str)
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", result["slug"]), (
        f"slug should be 22 URL-safe chars when slug=None, got: {result['slug']!r}"
    )
    assert re.search(r"/admin/notebooks/[A-Za-z0-9_-]{22}/data/x\.csv$", captured_paths[0]), (
        f"host URL path mismatch: {captured_paths[0]!r}"
    )


async def test_attach_refunds_rate_limit_slot_on_host_error() -> None:
    """A failing host PUT must refund the rate-limit slot.

    Without the refund, a flapping host locks the principal out of their
    hourly quota after a handful of retries even though no work succeeded.
    """
    settings = NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"),
        admin_secret=SecretStr("secret"),
    )
    limiter = RateLimiter(max_requests=2)

    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503, text="upstream busy")  # transient failure
        # Subsequent calls succeed.
        parts = req.url.path.rstrip("/").split("/")
        name = parts[-1]
        slug = parts[-3]
        return httpx.Response(
            200,
            json={
                "slug": slug,
                "name": name,
                "size_bytes": len(req.content),
                "path": f"data/{name}",
            },
        )

    async with _make_client(handler) as client:
        # First attempt fails; refund means the slot is given back.
        with pytest.raises(NotebookHostError):
            await attach_notebook_data(
                content=b"hello",
                name="x.csv",
                notebook_settings=settings,
                client=client,
                principal_key="acct-1",
                rate_limiter=limiter,
            )
        assert limiter.remaining("acct-1") == 2, (
            "failed host call should refund the slot — remaining must be 2, not 1"
        )

        # Now spend both slots successfully — proves the refund actually returned the budget.
        await attach_notebook_data(
            content=b"hello",
            name="y.csv",
            notebook_settings=settings,
            client=client,
            principal_key="acct-1",
            rate_limiter=limiter,
        )
        await attach_notebook_data(
            content=b"hello",
            name="z.csv",
            notebook_settings=settings,
            client=client,
            principal_key="acct-1",
            rate_limiter=limiter,
        )
        with pytest.raises(NotebookRateLimitError):
            await attach_notebook_data(
                content=b"hello",
                name="overflow.csv",
                notebook_settings=settings,
                client=client,
                principal_key="acct-1",
                rate_limiter=limiter,
            )
