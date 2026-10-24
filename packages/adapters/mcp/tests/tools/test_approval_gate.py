"""Agents and credentials cannot reach an approval-gate setter over MCP."""

from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.tools.channel_rules import register_channel_rule_tools
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError


async def test_approval_gate_setter_is_not_registered_or_callable() -> None:
    runtime = MagicMock()
    mcp = FastMCP("native approval controls only")
    register_channel_rule_tools(mcp, runtime)
    assert "set_approval_gate" not in {tool.name for tool in await mcp.local_provider.list_tools()}
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="[Uu]nknown|[Nn]ot found"):
            await client.call_tool("set_approval_gate", {"value": "off"})
    runtime.session_factory.assert_not_called()
