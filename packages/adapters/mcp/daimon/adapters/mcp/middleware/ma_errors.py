"""Tool-dispatch error boundary for upstream Anthropic SDK failures.

The MCP tool ``_*_impl`` functions call the Managed Agents API directly and let
``anthropic.APIError`` propagate (per guideline:architecture — no defensive
catches in the impls). Without a boundary, a 429/500 from MA or a connection
drop surfaces to the MCP caller as an opaque internal error.

``MaErrorMiddleware`` is that boundary: it wraps tool dispatch and converts
``anthropic.APIError`` into a structured ``ToolError``. It catches ONLY the SDK
error taxonomy — ``ToolError`` and everything else (including tools that already
converted ``BadRequestError`` to their own ``ToolError``) pass through unchanged.
"""

from __future__ import annotations

import anthropic
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

import mcp.types as mt


class MaErrorMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except anthropic.APIStatusError as exc:
            raise ToolError(
                f"The Managed Agents API returned an error (HTTP {exc.status_code}). "
                f"Please try again shortly."
            ) from exc
        except anthropic.APIError as exc:
            # APIConnectionError / APITimeoutError and other non-status SDK errors.
            raise ToolError(
                "Could not reach the Managed Agents API (connection error). "
                "Please try again shortly."
            ) from exc
