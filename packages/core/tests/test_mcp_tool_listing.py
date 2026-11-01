"""An MCP server's tool list: pages, hints, the ten-minute reuse, and refusals."""

from __future__ import annotations

import json

import httpx
import pytest
from daimon.core.mcp_tool_listing import ListedTool, McpToolLister, McpToolListingError, parse_tools
from daimon.core.turn.mcp_context import ContextServer

_SERVER = ContextServer(name="docs", url="https://docs.example.com/mcp", token="tok")


def _lister(pages: dict[str, dict[str, object]], calls: list[str] | None = None) -> McpToolLister:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if calls is not None:
            calls.append(method)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        cursor = body["params"].get("cursor", "")
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": pages[cursor]}
        )

    return McpToolLister(transport=httpx.MockTransport(handler))


def test_parse_tools_keeps_hints_and_skips_nameless_entries() -> None:
    parsed = parse_tools(
        [
            {"name": "a", "description": "x" * 900, "annotations": {"readOnlyHint": True}},
            {"description": "no name"},
            "junk",
            {"name": "b", "annotations": {"destructiveHint": "yes"}},
        ]
    )
    assert [(t.name, len(t.description), t.read_only_hint) for t in parsed] == [
        ("a", 500, True),
        ("b", 0, None),
    ], "long descriptions are cut, and a hint that is not a boolean is no hint"
    assert parsed[1].destructive_hint is None


async def test_listing_follows_pages_and_remembers_the_list() -> None:
    calls: list[str] = []
    lister = _lister(
        {
            "": {"tools": [{"name": "a"}], "nextCursor": "p2"},
            "p2": {"tools": [{"name": "b", "annotations": {"readOnlyHint": True}}]},
        },
        calls,
    )
    tools = await lister.list_tools(_SERVER)
    assert tools == (ListedTool(name="a"), ListedTool(name="b", read_only_hint=True))
    await lister.list_tools(_SERVER)
    assert calls.count("tools/list") == 2, "the second view is served from memory"


async def test_a_server_that_fails_or_answers_oddly_raises_one_error() -> None:
    broken = _lister({"": {"tools": "nope"}})
    with pytest.raises(McpToolListingError):
        await broken.list_tools(_SERVER)
    refused = McpToolLister(
        transport=httpx.MockTransport(lambda _request: httpx.Response(401)),
    )
    with pytest.raises(McpToolListingError):
        await refused.list_tools(_SERVER)


async def test_a_private_address_is_never_called() -> None:
    calls: list[str] = []
    lister = _lister({"": {"tools": []}}, calls)
    local = ContextServer(name="x", url="https://127.0.0.1/mcp", token="t")
    with pytest.raises(McpToolListingError):
        await lister.list_tools(local)
    assert calls == [], "no request left"
