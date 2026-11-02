"""An MCP server's tool list, read with one agent's shared token.

One `initialize`, then `tools/list` until the pages end. Lists are kept for
ten minutes per server and token, so a page of agents costs a server a request
each ten minutes, not one per view. A failed read raises `McpToolListingError`,
and is not tried again for a minute; the caller decides what to show instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
from daimon.core.errors import DaimonError
from daimon.core.mcp_oauth.urls import McpUrlError, assert_public_https_url
from daimon.core.mcp_rpc import mcp_headers, mcp_request, open_mcp_session
from daimon.core.turn.mcp_context import REUSE_S, ContextServer, Expiring
from pydantic import BaseModel, ConfigDict, JsonValue

_PAGES = 10
#: A failed read is not tried again for this long.
FAILED_REUSE_S = 60.0
_DESCRIPTION_CHARS = 500


class McpToolListingError(DaimonError):
    """The server could not be asked, or did not answer with a tool list."""


class ListedTool(BaseModel):
    """One tool a server offers, with the hints it gave about it."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None


def parse_tools(tools: list[JsonValue]) -> list[ListedTool]:
    """The tools of one `tools/list` page; entries without a name are skipped. Pure."""
    found: list[ListedTool] = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(name := tool.get("name"), str) or not name:
            continue
        description = tool.get("description")
        hints = tool.get("annotations")
        hints = hints if isinstance(hints, dict) else {}
        read_only, destructive = hints.get("readOnlyHint"), hints.get("destructiveHint")
        found.append(
            ListedTool(
                name=name,
                description=description[:_DESCRIPTION_CHARS]
                if isinstance(description, str)
                else "",
                read_only_hint=read_only if isinstance(read_only, bool) else None,
                destructive_hint=destructive if isinstance(destructive, bool) else None,
            )
        )
    return found


@dataclass
class McpToolLister:
    """Lists servers' tools, remembering a list for `REUSE_S`, a failure for `FAILED_REUSE_S`."""

    timeout_s: float = 10.0
    transport: httpx.AsyncBaseTransport | None = None
    cache: Expiring[tuple[str, str], tuple[ListedTool, ...]] = field(
        default_factory=lambda: Expiring[tuple[str, str], tuple[ListedTool, ...]]()
    )
    failures: Expiring[tuple[str, str], bool] = field(
        default_factory=lambda: Expiring[tuple[str, str], bool]()
    )

    async def list_tools(
        self, server: ContextServer, *, cache_as: tuple[str, str] | None = None
    ) -> tuple[ListedTool, ...]:
        """The tools ``server`` offers to the holder of its token.

        ``cache_as`` keeps the list under a holder's key instead of the token's,
        for a token minted afresh for each read.
        """
        key = cache_as or server.key
        known = self.cache.recall(key)
        if known is not None:
            return known
        if self.failed_recently(key):
            raise McpToolListingError(f"{server.name} did not list its tools just now")
        try:
            assert_public_https_url(server.url, what="connection URL")
            async with (
                asyncio.timeout(self.timeout_s),
                # No redirects: the configured URL is the only authority.
                httpx.AsyncClient(
                    timeout=self.timeout_s, follow_redirects=False, transport=self.transport
                ) as http,
            ):
                tools = await _read(http, server)
        except (httpx.HTTPError, ValueError, McpUrlError, TimeoutError) as err:
            self.failures.remember(key, True, for_s=FAILED_REUSE_S)
            raise McpToolListingError(f"{server.name} did not list its tools") from err
        self.cache.remember(key, tools, for_s=REUSE_S)
        return tools

    def failed_recently(self, key: tuple[str, str]) -> bool:
        """Whether reading ``key``'s list failed within `FAILED_REUSE_S`."""
        return self.failures.recall(key) is not None


async def _read(http: httpx.AsyncClient, server: ContextServer) -> tuple[ListedTool, ...]:
    headers = mcp_headers(server.token)
    await open_mcp_session(http, server.url, headers)
    tools: list[ListedTool] = []
    cursor: str | None = None
    for _ in range(_PAGES):
        body = {
            "jsonrpc": "2.0",
            "id": f"tools-{len(tools)}-{cursor or ''}",
            "method": "tools/list",
            "params": {"cursor": cursor} if cursor else {},
        }
        result = await mcp_request(http, server.url, headers, body)
        page = result.get("tools")
        if not isinstance(page, list):
            raise ValueError("invalid MCP tool catalogue")
        tools.extend(parse_tools(page))
        following = result.get("nextCursor")
        if not isinstance(following, str) or not following:
            break
        cursor = following
    return tuple(tools)
