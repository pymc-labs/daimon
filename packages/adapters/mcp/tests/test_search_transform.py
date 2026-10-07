"""The search collapse keeps tools that wait for the person's Approve off `call_tool`.

Managed Agents applies a session's per-tool `always_ask` to the name the model
calls. Through the proxy that name is `call_tool`, which daimon's toolset
allows, so a gated tool reached that way would run without a card.
"""

from __future__ import annotations

import pytest
from daimon.adapters.mcp.search_transform import APPROVAL_TOOLS, AgentChatAwareBM25SearchTransform
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

_GATED = sorted(APPROVAL_TOOLS)


def _server() -> FastMCP:
    mcp = FastMCP("test")
    calls: list[str] = []

    def register(name: str) -> None:
        def tool() -> str:
            calls.append(name)
            return name

        mcp.tool(tool, name=name)

    for name in [*_GATED, "list_agents"]:
        register(name)
    mcp.add_transform(AgentChatAwareBM25SearchTransform(max_results=5))
    mcp.calls = calls  # pyright: ignore[reportAttributeAccessIssue]
    return mcp


async def test_gated_tools_are_listed_directly() -> None:
    async with Client(_server()) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"search_tools", "call_tool", *_GATED}, (
        "a gated tool must be listed under its own name so its always_ask applies"
    )


async def test_search_still_finds_gated_tools() -> None:
    async with Client(_server()) as client:
        result = await client.call_tool("search_tools", {"query": "add skill"})
    assert "add_skill" in str(result.content), "searching for a gated tool must still find it"


@pytest.mark.parametrize("name", _GATED)
async def test_call_tool_refuses_gated_tool(name: str) -> None:
    mcp = _server()
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match=f"Call {name} directly"):
            await client.call_tool("call_tool", {"name": name, "arguments": {}})
    assert mcp.calls == [], "a proxied gated call must not reach the tool"  # pyright: ignore[reportAttributeAccessIssue]


async def test_call_tool_still_runs_other_tools() -> None:
    mcp = _server()
    async with Client(mcp) as client:
        await client.call_tool("call_tool", {"name": "list_agents", "arguments": {}})
    assert mcp.calls == ["list_agents"]  # pyright: ignore[reportAttributeAccessIssue]
