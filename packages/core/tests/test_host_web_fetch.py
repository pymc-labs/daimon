"""Daimon's own page fetch (`daimon.core.host_web_fetch`) through the pinned client."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from daimon.core.host_web_fetch import (
    MAX_FETCH_BYTES,
    MAX_REDIRECTS,
    MAX_TEXT_CHARS,
    WebFetchError,
    fetch_page,
    html_text,
    page_text,
)
from daimon.core.mcp_tool_call import McpCallError, pinned_http_client

_PRIVATE = "private.example"
Handler = Callable[[httpx.Request], httpx.Response]


async def _resolve(host: str, port: int) -> str:
    if host == _PRIVATE:
        raise McpCallError("address")
    return "93.184.216.34"


def _client(handler: Handler) -> httpx.AsyncClient:
    return pinned_http_client(resolve=_resolve, transport=httpx.MockTransport(handler))


def test_html_text_when_page_has_scripts_then_only_readable_lines() -> None:
    html = "<html><style>a{}</style><h1>Title</h1><p>One &amp; two</p><script>x()</script></html>"
    assert html_text(html) == "Title\nOne & two", "scripts and styles out, one line per block"


def test_page_text_when_long_or_truncated_then_cut_short() -> None:
    long = page_text(b"a" * (MAX_TEXT_CHARS + 1), "text/plain", None, truncated=False)
    assert long.endswith("[Page cut short.]") and len(long) < MAX_TEXT_CHARS + 40
    short = page_text(b"abc", "text/plain", None, truncated=True)
    assert short == "abc\n[Page cut short.]", "a capped body says so"


async def test_fetch_page_when_redirected_then_follows_to_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, html="<p>Hello</p>")

    async with _client(handler) as client:
        assert await fetch_page("https://site.example/old", client=client) == "Hello"


@pytest.mark.parametrize(
    ("url", "response", "message"),
    [
        ("http://site.example/", httpx.Response(200, text="x"), "public https"),
        ("https://localhost/", httpx.Response(200, text="x"), "public https"),
        ("https://site.example/", httpx.Response(200, content=b"\x89PNG"), "not text"),
        ("https://site.example/", httpx.Response(404), "answered 404"),
        (
            "https://site.example/",
            httpx.Response(302, headers={"location": f"https://{_PRIVATE}/"}),
            "public https",
        ),
        (
            "https://site.example/",
            httpx.Response(302, headers={"location": "http://site.example/"}),
            "public https",
        ),
    ],
)
async def test_fetch_page_when_unsafe_or_unusable_then_refused(
    url: str, response: httpx.Response, message: str
) -> None:
    async with _client(lambda _: response) as client:
        with pytest.raises(WebFetchError, match=message):
            await fetch_page(url, client=client)


async def test_fetch_page_when_redirects_loop_then_refused() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(302, headers={"location": "/again"})

    async with _client(handler) as client:
        with pytest.raises(WebFetchError, match="too many"):
            await fetch_page("https://site.example/", client=client)
    assert len(calls) == MAX_REDIRECTS + 1, "redirects are capped"


async def test_fetch_page_when_body_is_huge_then_capped() -> None:
    body = b"a" * (MAX_FETCH_BYTES + 10)
    response = httpx.Response(200, content=body, headers={"content-type": "text/plain"})
    async with _client(lambda _: response) as client:
        text = await fetch_page("https://site.example/", client=client)
    assert text.endswith("[Page cut short.]"), "an oversized body is cut short"
