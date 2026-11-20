"""Eval callers never reach a tool handler: the fake world answers every call they make.

A caller is an eval caller when its identity is the eval tenant's. Its item is the one its
credential's registry row is bound to (`AuthIdentity.eval_item_id`); without a running item the
call is refused, never run. Innermost, so every gate before it still applies; the search
proxies pass through because `call_tool` re-enters with the inner tool's name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.eval_mcp import EVAL_WORLD_TAG, tool_texts
from daimon.core.evals.eval_tenant import is_eval_tenant
from daimon.core.evals.format import ExchangeResponse, McpTool
from daimon.core.evals.world.dispatch import DAIMON_SERVER, FakeWorld, ItemNotRunningError
from daimon.core.evals.world.services.web import SEARCH_TOOL
from daimon.core.host_web_fetch import HOST_WEB_FETCH_TAG
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms.visibility import enable_components
from fastmcp.tools.base import Tool, ToolResult

import mcp.types as mt
from mcp.types import TextContent

#: The search transform's proxies: they re-enter this middleware with the real tool.
PROXY_TOOLS: Final = frozenset({"search_tools", "call_tool"})
_NO_ITEM: Final = "This eval item isn't running, so nothing can answer. Nothing was done."


def _result(response: ExchangeResponse) -> ToolResult:
    texts = tool_texts(response)
    if response.is_error:
        raise ToolError("\n".join(texts) or "The tool failed.")
    structured = response.result if isinstance(response.result, dict) else None
    return ToolResult(
        content=[TextContent(type="text", text=t) for t in texts], structured_content=structured
    )


class EvalWorldMiddleware(Middleware):
    def __init__(self, world: FakeWorld) -> None:
        self._world = world

    @staticmethod
    async def _eval_caller(ctx: Context | None) -> AuthIdentity | None:
        auth = await ctx.get_state("auth") if ctx is not None else None
        if not isinstance(auth, AuthIdentity) or not is_eval_tenant(auth.tenant_id):
            return None
        assert ctx is not None
        await enable_components(ctx, tags={HOST_WEB_FETCH_TAG, EVAL_WORLD_TAG})
        return auth

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        await self._eval_caller(context.fastmcp_context)
        return await call_next(context)

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        ctx = context.fastmcp_context
        auth = await self._eval_caller(ctx)
        name = context.message.name
        if auth is None or name in PROXY_TOOLS:
            return await call_next(context)
        assert ctx is not None
        item = await self._world.item(auth.eval_item_id) if auth.eval_item_id else None
        if item is None:
            raise ToolError(_NO_ITEM)
        tool = await ctx.fastmcp.get_tool(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        args = dict(context.message.arguments or {})
        info = McpTool(name=name, description=tool.description, input_schema=tool.parameters,
                       output_schema=tool.output_schema)  # fmt: skip
        try:
            if name == SEARCH_TOOL:
                answer = await self._world.provider_tool(item, name, args)
            else:
                answer = await self._world.mcp(item, DAIMON_SERVER, name, args, info)
        except ItemNotRunningError as exc:
            raise ToolError(_NO_ITEM) from exc
        return _result(answer.response)
