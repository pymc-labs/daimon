"""Probe FastMCP 3.x middleware → tool handler state handoff + ToolError surface.

Questions:
1. How does middleware stash per-request state for tool handlers to read?
   (Spec draft assumes `context.fastmcp_context.state["auth"]` — that is not
   the 3.x API. Establish the correct incantation.)
2. What does raising `ToolError` from a handler look like on the wire?
3. What does raising from middleware look like on the wire (auth reject)?

Run:
    uv run --with fastmcp --with pyjwt python scripts/probes/mcp/fastmcp_middleware_state.py
"""

from __future__ import annotations

import asyncio
import json

from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext


class AuthMiddleware(Middleware):
    """Stashes a fake AuthIdentity onto the request's Context via set_state."""

    async def on_request(self, context: MiddlewareContext, call_next):
        ctx: Context | None = context.fastmcp_context
        if ctx is not None:
            # Simulate: parse Authorization, verify JWT, resolve role.
            await ctx.set_state("auth", {"sub": "cli:testuser", "account_id": 42, "role": "admin"})
        return await call_next(context)


class RejectingMiddleware(Middleware):
    """Rejects at tool-call time (not initialize) — matches how auth should be wired."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        raise ToolError("missing bearer")


def build_ok_app() -> FastMCP:
    mcp = FastMCP("probe-ok")
    mcp.add_middleware(AuthMiddleware())

    @mcp.tool
    async def whoami(ctx: Context) -> dict:
        auth = await ctx.get_state("auth")
        return {"auth": auth}

    @mcp.tool
    def boom() -> str:
        raise ToolError("not found")

    @mcp.tool
    def kaboom() -> str:
        raise RuntimeError("unexpected internal error")

    return mcp


def build_rejecting_app() -> FastMCP:
    mcp = FastMCP("probe-reject")
    mcp.add_middleware(RejectingMiddleware())

    @mcp.tool
    def whoami() -> str:
        return "unreachable"

    return mcp


async def drive(mcp: FastMCP, label: str) -> None:
    print(f"\n===== {label} =====")
    async with Client(mcp) as client:
        # list_tools
        tools = await client.list_tools()
        print(f"tools: {[t.name for t in tools]}")

        for tool_name in ["whoami", "boom", "kaboom"]:
            if tool_name not in {t.name for t in tools}:
                continue
            try:
                result = await client.call_tool(tool_name, {})
                print(f"\n-- call {tool_name} --")
                print(f"  is_error: {result.is_error}")
                print(f"  content: {[c.model_dump() if hasattr(c, 'model_dump') else c for c in result.content]}")
                print(f"  structured: {result.structured_content}")
            except Exception as e:  # noqa: BLE001
                print(f"\n-- call {tool_name} RAISED --")
                print(f"  type={type(e).__name__} msg={e}")


async def main() -> None:
    await drive(build_ok_app(), "happy path: middleware sets state, tools raise/return")
    await drive(build_rejecting_app(), "middleware rejects: ToolError from on_request")
    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
