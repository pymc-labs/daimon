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
            raise ToolError(_status_message(exc.status_code)) from exc
        except anthropic.APIError as exc:
            # APIConnectionError / APITimeoutError and other non-status SDK errors.
            raise ToolError(
                "Couldn't reach Daimon's AI service (connection error). Try again shortly."
            ) from exc


def _status_message(status: int) -> str:
    """Plain words for an HTTP status: only a busy or failing service is worth retrying."""
    if status == 429 or status >= 500:
        return f"Daimon's AI service is busy or failing (HTTP {status}). Try again shortly."
    if status in (401, 403):
        return (
            f"The AI provider rejected Daimon's request (HTTP {status}). Trying again won't "
            "help: the team running Daimon needs to check its key for this provider."
        )
    if status == 404:
        return (
            f"Daimon's AI service couldn't find what this asked for (HTTP {status}); "
            "it may have been deleted. Trying again won't help."
        )
    return (
        f"Daimon's AI service refused this request (HTTP {status}). Trying again won't "
        "help; change the request first."
    )
