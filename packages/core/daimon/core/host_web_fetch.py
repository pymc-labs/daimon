"""Daimon-hosted page fetch for backends with no native web_fetch tool.

Every request goes through `pinned_http_client`: public https only, each
redirect judged like the first URL, one pinned address per host. Bodies are
capped in bytes and the text returned is capped in characters.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Final
from urllib.parse import urljoin

import httpx
from daimon.core.host_memory import HOST_MEMORY_TOOLS
from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_https_url
from daimon.core.mcp_tool_call import AddressRefusedError

WEB_FETCH_TOOL: Final = "web_fetch"
HOST_WEB_FETCH_TAG: Final = "host-web-fetch"
PINNED_HOST_TOOLS: Final = (*HOST_MEMORY_TOOLS, WEB_FETCH_TOOL)
"""Listed by name beside the search tools, so a provider turn's catalogue names them."""
MAX_FETCH_BYTES: Final = 2 * 1024 * 1024
MAX_TEXT_CHARS: Final = 100_000
MAX_REDIRECTS: Final = 5
FETCH_TIMEOUT_S: Final = 20.0
_TEXT_TYPES: Final = ("text/", "application/json", "application/xml", "application/xhtml+xml")
_SKIPPED: Final = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCKS: Final = frozenset(
    {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
)


class WebFetchError(ValueError):
    """A fetch refused or failed, with a reason the agent can act on."""


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skip += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED and self._skip:
            self._skip -= 1
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_text(html: str) -> str:
    """The readable text of an HTML page: scripts and styles out, one line per block."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def page_text(body: bytes, content_type: str, encoding: str | None, *, truncated: bool) -> str:
    """The text an agent reads for one fetched body, capped at `MAX_TEXT_CHARS`."""
    text = body.decode(encoding or "utf-8", errors="replace")
    if "html" in content_type:
        text = html_text(text)
    if truncated or len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n[Page cut short.]"
    return text


async def _read(response: httpx.Response) -> tuple[bytes, bool]:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > MAX_FETCH_BYTES:
            return bytes(body[:MAX_FETCH_BYTES]), True
    return bytes(body), False


async def fetch_page(url: str, *, client: httpx.AsyncClient) -> str:
    """The text of the page at `url`, following at most `MAX_REDIRECTS` redirects."""
    try:
        assert_public_https_url(url, what="URL")
    except McpUrlError:
        raise WebFetchError("Only public https pages can be opened.") from None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            async with client.stream("GET", url) as response:
                location = response.headers.get("location")
                if response.is_redirect and location:
                    url = urljoin(url, location)
                    continue
                if not response.is_success:
                    raise WebFetchError(f"The page answered {response.status_code}.")
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith(_TEXT_TYPES):
                    raise WebFetchError("That page is not text.")
                body, truncated = await _read(response)
                return page_text(body, content_type, response.encoding, truncated=truncated)
        raise WebFetchError("The page redirected too many times.")
    except AddressRefusedError:
        raise WebFetchError("Only public https pages can be opened.") from None
    except httpx.TimeoutException:
        raise WebFetchError("The page took too long to answer.") from None
    except httpx.HTTPError:
        raise WebFetchError("The page could not be opened.") from None
