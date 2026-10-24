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


def _fleet_server(auth) -> FastMCP:
    from typing import Any

    from fastmcp.server.middleware import Middleware, MiddlewareContext

    class Identity(Middleware):
        async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
            await context.fastmcp_context.set_state("auth", auth, serializable=False)
            return await call_next(context)

    mcp = FastMCP("test")
    for name in ("fleet_spawn", "fleet_collect", "fleet_status", "fleet_agents", "fleet_kill"):

        def tool(name: str = name) -> str:
            return name

        mcp.tool(tool, name=name)
    mcp.add_middleware(Identity())
    mcp.add_transform(AgentChatAwareBM25SearchTransform(max_results=5))
    return mcp


@pytest.mark.parametrize("native", [True, False])
async def test_a_gpt_or_gemini_turn_sees_the_fleet_tools_by_name(native: bool) -> None:
    """M07 (diagnosis credit: dsh): a GPT/Gemini copy's native tool allowlist is built
    from this list, and with fleet tools only behind search_tools it reached for saved
    copies instead. A provider turn with no confirmation surface lists the tools that
    start and read a fleet; a Claude turn's list is unchanged (it searches)."""
    import uuid

    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.core.stores.domain import Role

    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        no_confirmation_surface=native,
    )
    async with Client(_fleet_server(auth)) as client:
        names = {tool.name for tool in await client.list_tools()}
        found = await client.call_tool("search_tools", {"query": "fleet kill worker"})
    pinned = {"fleet_spawn", "fleet_collect", "fleet_status", "fleet_agents"}
    assert names == {"search_tools", "call_tool", *(pinned if native else ())}
    # The rest stays reachable through search either way.
    assert "fleet_kill" in str(found.content)
