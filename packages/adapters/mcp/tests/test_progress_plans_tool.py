"""`update_plan` asks for nothing but the steps."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from daimon.adapters.mcp.tools.progress_plans import register_progress_plan_tools
from fastmcp import FastMCP


async def test_update_plan_asks_only_for_steps_and_acknowledges() -> None:
    """The schema holds steps alone: no field may invite the model's reasoning into a tool input."""
    mcp = FastMCP(name="t")
    register_progress_plan_tools(mcp, MagicMock())
    tool = next(tool for tool in await mcp.list_tools() if tool.name == "update_plan")
    schema = json.dumps(tool.parameters).lower()
    text = f"{schema} {(tool.description or '').lower()}"

    for word in ("reason", "explanation", "explain", "note", "thinking", "thought", "why"):
        assert word not in text, f"update_plan's schema or description must not mention {word!r}"
    assert set(tool.parameters["properties"]) == {"steps"}, "steps is the only argument"
    assert tool.tags == {"discord", "slack", "teams"}, "every chat platform sees the tool"

    result = await mcp.call_tool(
        "update_plan", {"steps": [{"step": "Load the data", "status": "in_progress"}]}
    )
    assert "not a reply" in json.dumps(result.content[0].model_dump()), (
        "the ack tells the model to carry on"
    )
