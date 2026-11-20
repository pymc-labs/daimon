"""The step tools act only for the run's run-as person, on a running step."""

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import workflow_step
from daimon.core.stores import workflow_runs
from daimon.core.stores.workflow_run_events import list_events
from daimon.core.workflows.step_tools import RECORDED
from daimon.testing.workflows import make_world
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession

from ..harness import auth_identity

NODES: dict[str, Any] = {
    "collect": {
        "kind": "agent",
        "instructions": "Count the deals",
        "output": {"count": {"type": "integer"}},
        "next": "done",
    },
    "done": {"kind": "finish"},
}


async def test_complete_step_records_for_the_owner_and_refuses_others(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world(db_session, NODES, "collect")
    run = await world.run()
    claimed = await world.claim(run.id, "collect")
    async with world.sm() as s:
        attempt = await workflow_runs.running_attempt(s, claimed.step.id)
    assert attempt is not None
    who = {"account": uuid.uuid4()}

    async def auth(_ctx: object) -> object:
        return auth_identity(who["account"], world.tenant_id)

    monkeypatch.setattr(workflow_step, "_auth", auth)
    mcp = FastMCP("t")
    runtime = SimpleNamespace(session_factory=world.sm)
    workflow_step.register_workflow_step_tools(mcp, cast(McpRuntime, runtime))
    tool = await mcp.get_tool("complete_step")
    assert tool is not None

    async def call(**kw: Any) -> str:
        return await tool.fn(ctx=None, step=attempt.id, summary="Counted", **kw)  # type: ignore[attr-defined]

    with pytest.raises(ToolError, match="not running any more"):
        await call(output={"count": 1})
    who["account"] = world.owner
    assert (await call(output={"count": "x"})).startswith("Not recorded"), "errors come back"
    assert await call(output={"count": 1}) == "Recorded. End your turn now."
    with pytest.raises(ToolError, match="already has a result"):
        await call(output={"count": 2})
    async with world.sm() as s:
        events = [e for e in await list_events(s, run.id) if e.type == RECORDED]
    assert [e.data["output"] for e in events] == [{"count": 1}], "one result on the ledger"
