from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from daimon_harness.runtime import TurnContext
from mux.drivers.selfhosted.wire import InlineSkill, TurnRequest
from pydantic import JsonValue

from .conftest import turn


def context(home: Path) -> TurnContext:
    return TurnContext(TurnRequest.model_validate(turn()), home, lambda kind, data: None)


async def test_workspace_tools_and_skills(tmp_path: Path) -> None:
    runtime = context(tmp_path)
    assert runtime.write("nested/a.txt", "one two") == "written"
    assert runtime.read("nested/a.txt") == "one two"
    assert runtime.edit("nested/a.txt", "two", "three") == "edited"
    assert runtime.read("nested/a.txt") == "one three"
    with pytest.raises(ValueError):
        runtime.edit("nested/a.txt", "missing", "new")
    with pytest.raises(ValueError):
        runtime.write("../escape", "bad")
    (runtime.work / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        runtime.read("link/secret")
    runtime.request.skills = [
        InlineSkill(
            name="analysis", files={"SKILL.md": "Use the analysis skill", "data.txt": "data"}
        )
    ]
    instructions = await runtime.skills()
    assert "Use the analysis skill" in instructions
    assert (tmp_path / "skills/analysis/data.txt").read_text() == "data"


async def test_bash_cwd_credentials_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = context(tmp_path)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "should-not-be-visible")
    output = await runtime.bash('pwd; test -z "$AZURE_OPENAI_API_KEY" && echo isolated')
    assert str(runtime.work) in output
    assert "isolated" in output
    task = asyncio.create_task(runtime.bash("sleep 120"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)


async def test_tool_limit_blocks_execution(tmp_path: Path) -> None:
    runtime = context(tmp_path)
    runtime.request.limits.max_tool_calls = 0
    called = False

    async def execute() -> JsonValue:
        nonlocal called
        called = True
        return "unexpected"

    with pytest.raises(RuntimeError, match="maximum tool calls"):
        await runtime.tool("c", "read", {}, execute)
    assert not called
