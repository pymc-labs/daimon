"""Approvals change over MCP only through `change_setting`: carded, native-admin only.

There is still no raw `set_approval_gate` tool; the one setter is the Settings group's,
whose bypass refusals are in `test_scope_settings_tool.py`.
"""

from unittest.mock import MagicMock

import pytest
from daimon.adapters.mcp.tools.channel_rules import register_channel_rule_tools
from daimon.adapters.mcp.tools.scope_settings import register_scope_settings_tools
from daimon.core.tool_safety import CARDED_TOOLS, PERMISSION_SETTER_TOOLS, gate_protected_tool
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError


async def test_raw_approval_gate_setter_is_not_registered_or_callable() -> None:
    runtime = MagicMock()
    mcp = FastMCP("native approval controls only")
    register_channel_rule_tools(mcp, runtime)
    register_scope_settings_tools(mcp, runtime)
    assert "set_approval_gate" not in {tool.name for tool in await mcp.local_provider.list_tools()}
    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="[Uu]nknown|[Nn]ot found"):
            await client.call_tool("set_approval_gate", {"value": "off"})
    runtime.session_factory.assert_not_called()


async def test_the_approvals_setter_is_carded_and_protected() -> None:
    mcp = FastMCP("settings")
    register_scope_settings_tools(mcp, MagicMock())
    tools = {tool.name: tool for tool in await mcp.local_provider.list_tools()}
    assert "approvals" in tools["change_setting"].parameters["properties"]["key"]["enum"]
    assert CARDED_TOOLS["change_setting"] == "origin_context_id"
    assert "change_setting" in PERMISSION_SETTER_TOOLS
    assert gate_protected_tool("change_setting")
