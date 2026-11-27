"""The search tool eval agents get in place of a provider's own.

Only eval callers see it, and the fake world always answers it (`middleware/eval_world.py`).
"""

from __future__ import annotations

from daimon.adapters.mcp.eval_mcp import EVAL_WORLD_TAG
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


def register_eval_world_tools(mcp: FastMCP) -> None:
    @mcp.tool(tags={EVAL_WORLD_TAG})
    async def web_search(query: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Search the public web and return the top results: title, link and a short extract."""
        raise ToolError("This tool isn't available right now. Nothing was done.")
