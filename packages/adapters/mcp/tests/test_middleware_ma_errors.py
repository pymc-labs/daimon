"""Tests for MaErrorMiddleware — converts upstream Anthropic SDK errors raised
inside a tool into a structured ToolError at the MCP dispatch boundary (#14)."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx
import pytest
from daimon.adapters.mcp.middleware.ma_errors import MaErrorMiddleware
from fastmcp.exceptions import ToolError

pytestmark = pytest.mark.asyncio


async def test_on_call_tool_converts_api_status_error_to_tool_error() -> None:
    mw = MaErrorMiddleware()

    async def call_next(_ctx: Any) -> Any:
        raise anthropic.APIStatusError(
            "rate limited",
            response=httpx.Response(
                429, request=httpx.Request("POST", "https://api.anthropic.com/v1/x")
            ),
            body=None,
        )

    with pytest.raises(ToolError, match="429"):
        await mw.on_call_tool(object(), call_next)  # type: ignore[arg-type]


async def test_on_call_tool_converts_connection_error_to_tool_error() -> None:
    mw = MaErrorMiddleware()

    async def call_next(_ctx: Any) -> Any:
        raise anthropic.APIConnectionError(
            request=httpx.Request("POST", "https://api.anthropic.com/v1/x")
        )

    with pytest.raises(ToolError, match="reach"):
        await mw.on_call_tool(object(), call_next)  # type: ignore[arg-type]


async def test_on_call_tool_passes_through_non_anthropic_result() -> None:
    mw = MaErrorMiddleware()

    async def call_next(_ctx: Any) -> str:
        return "ok"

    assert await mw.on_call_tool(object(), call_next) == "ok"  # type: ignore[arg-type]


async def test_on_call_tool_does_not_swallow_tool_error() -> None:
    """A ToolError raised by the tool itself must pass through unchanged."""
    mw = MaErrorMiddleware()

    async def call_next(_ctx: Any) -> Any:
        raise ToolError("agent 'x' not found")

    with pytest.raises(ToolError, match="not found"):
        await mw.on_call_tool(object(), call_next)  # type: ignore[arg-type]
