"""BM25SearchTransform wiring smoke test.

Builds a minimal in-memory FastMCP server with stub tools and asserts
list_tools() returns only the two meta-tools plus pinned vault tool.
Does NOT test BM25 ranking, call_tool delegation, or markdown format —
those are FastMCP's responsibility (validated in spikes 004-006).
"""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.server.transforms.search.base import serialize_tools_for_output_markdown

pytestmark = pytest.mark.asyncio


async def test_bm25_transform_collapses_catalog() -> None:
    mcp = FastMCP("test")

    @mcp.tool
    def list_agents() -> list[str]:
        """List agents."""
        return []

    @mcp.tool
    def create_agent(name: str) -> str:
        """Create an agent."""
        return name

    @mcp.tool
    def list_environments() -> list[str]:
        """List environments."""
        return []

    @mcp.tool
    def list_credentials() -> list[str]:
        """List vault credentials."""
        return []

    mcp.add_transform(
        BM25SearchTransform(
            max_results=5,
            always_visible=["list_credentials"],
            search_result_serializer=serialize_tools_for_output_markdown,
        )
    )

    async with Client(mcp) as client:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert tool_names == {"search_tools", "call_tool", "list_credentials"}, (
            f"expected meta-tools + pinned vault tool, got {tool_names}"
        )
