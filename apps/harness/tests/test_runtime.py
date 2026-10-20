from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from daimon_harness.errors import HarnessError
from daimon_harness.runtime import TurnContext, discard_event
from mux.drivers.selfhosted.wire import InlineSkill, TurnRequest
from pydantic import JsonValue

from .conftest import turn


def context(home: Path) -> TurnContext:
    return TurnContext(TurnRequest.model_validate(turn()), home, discard_event)


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

    with pytest.raises(HarnessError, match="Maximum tool calls"):
        await runtime.tool("c", "read", {}, execute)
    assert not called


async def test_cancel_during_spawn_waits_for_process_cleanup(tmp_path, monkeypatch):
    import os
    import signal
    from contextlib import suppress

    runtime = context(tmp_path)
    create = asyncio.create_subprocess_exec
    started = asyncio.Event()
    release = asyncio.Event()
    children = []

    async def delayed_spawn(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        started.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    task = asyncio.create_task(runtime.bash("sleep 120"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert not runtime.process_groups
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancel must wait for the in-flight spawn"
        task.cancel()  # Repeated cancellation must not cancel the cleanup task.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert children[0].returncode is not None
        assert not runtime.process_groups
        with pytest.raises(ProcessLookupError):
            os.kill(children[0].pid, 0)
    finally:
        release.set()
        for child in children:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            await child.wait()
        await asyncio.gather(task, return_exceptions=True)


async def test_bash_bounds_output_and_cancels_a_flooded_pipe(tmp_path: Path) -> None:
    runtime = context(tmp_path)
    output = await runtime.bash("printf '%0400000d' 0")
    assert output.startswith("exit_code=0\n")
    assert len(output) < 262_244
    assert "[output truncated]" in output
    task = asyncio.create_task(runtime.bash("while true; do printf '%010000d' 0; done"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)


async def test_binary_skill_cache_and_replacement(tmp_path: Path) -> None:
    import base64

    runtime = context(tmp_path)
    skill = InlineSkill(
        name="marimo_notebooks",
        files={"SKILL.md": "first", "old.txt": "old"},
        binary_files={"fonts/font.bin": base64.b64encode(b"\x00\xff").decode()},
    )
    runtime.request.skills = [skill]
    await runtime.skills()
    directory = tmp_path / "skills" / skill.name
    binary = directory / "fonts/font.bin"
    assert binary.read_bytes() == b"\x00\xff"
    binary.stat()
    before = binary.stat().st_mtime_ns
    assert "first" in await runtime.skills()
    assert binary.stat().st_mtime_ns == before
    marker = tmp_path / ".daimon/skills" / (skill.name + ".sha256")
    assert marker.read_text() == skill.sha256
    runtime.request.skills = [InlineSkill(name=skill.name, files={"SKILL.md": "second"})]
    assert "second" in await runtime.skills()
    assert not binary.exists()
    assert not (directory / "old.txt").exists()
    assert marker.read_text() == runtime.request.skills[0].sha256


async def test_skill_materialization_rejects_existing_symlinks(tmp_path: Path) -> None:
    runtime = context(tmp_path)
    runtime.request.skills = [InlineSkill(name="analysis", files={"SKILL.md": "safe"})]
    outside = tmp_path / "outside"
    outside.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "analysis").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        await runtime.skills()
    assert list(outside.iterdir()) == []


async def test_skill_digest_rechecked_before_materializing(tmp_path: Path) -> None:
    runtime = context(tmp_path)
    skill = InlineSkill(name="analysis", files={"SKILL.md": "safe"})
    skill.files["SKILL.md"] = "tampered"
    runtime.request.skills = [skill]
    with pytest.raises(ValueError, match="sha256"):
        await runtime.skills()
    assert not (tmp_path / "skills/analysis").exists()


async def test_builtin_tools_default_to_auto_and_explicit_ask_is_required(tmp_path: Path) -> None:
    from mux.drivers.selfhosted.wire import ActionRequest

    runtime = context(tmp_path)

    async def execute() -> JsonValue:
        return runtime.write("approved.txt", "yes")

    assert await runtime.tool("auto-call", "write", {}, execute) == "written"
    runtime.request.tool_policy = {"write": "ask"}
    (runtime.work / "approved.txt").unlink()
    pending = asyncio.create_task(runtime.tool("ask-call", "write", {}, execute))
    await asyncio.sleep(0)
    assert not (runtime.work / "approved.txt").exists()
    action_id = next(iter(runtime.pending))
    runtime.pending[action_id].set_result(ActionRequest(action_id=action_id, decision="allow"))
    assert await pending == "written"
    assert (runtime.work / "approved.txt").read_text() == "yes"


async def test_tool_runtime_error_is_paired_and_next_call_runs(tmp_path: Path) -> None:
    runtime = context(tmp_path)
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def emit(kind, data):
        events.append((kind, data))

    runtime.emit = emit

    async def broken() -> JsonValue:
        raise RuntimeError("private upstream details")

    assert await runtime.tool("bad", "remote", {}, broken, mcp=True) == "Tool failed: RuntimeError"
    assert events[-1] == (
        "tool_result",
        {"call_id": "bad", "output": "Tool failed: RuntimeError", "is_error": True},
    )

    async def working() -> JsonValue:
        return "ok"

    assert await runtime.tool("good", "read", {}, working) == "ok"
    runtime.request.limits.max_tool_calls = 2
    with pytest.raises(HarnessError, match="Maximum tool calls"):
        await runtime.tool("limit", "read", {}, working)
    assert events[-2][0] == "tool_use"
    assert events[-1][1]["call_id"] == "limit"
    assert events[-1][1]["is_error"] is True


async def test_shell_deadline_and_success_reap_background_jobs(tmp_path: Path) -> None:
    runtime = context(tmp_path)
    runtime.request.limits.deadline_s = 0.1
    with pytest.raises(TimeoutError):
        await runtime.bash("sleep 60")
    runtime.request.limits.deadline_s = 5
    output = await runtime.bash("nohup sleep 60 >/dev/null 2>&1 & echo $!")
    pid = int(output.splitlines()[-1])
    async with asyncio.timeout(3):
        while True:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            await asyncio.sleep(0.01)


async def test_denied_tools_are_hidden_and_refused_with_exact_precedence(tmp_path: Path) -> None:
    from daimon_harness.model_adapter import builtin_tools

    runtime = context(tmp_path)
    runtime.request.tool_policy = {
        "bash": "deny",
        "sample/*": "deny",
        "sample/search": "auto",
        "sample/ask": "ask",
    }
    assert {tool.name for tool in builtin_tools(runtime)} == {"read", "write", "edit"}
    assert runtime.permission("sample/search") == "auto"
    assert runtime.permission("sample/ask") == "ask"
    assert runtime.permission("sample/delete") == "deny"
    assert runtime.permission("other/search") == "auto"
    executed = False

    async def execute() -> JsonValue:
        nonlocal executed
        executed = True
        return "unexpected"

    for name in ("bash", "sample/delete"):
        assert await runtime.tool(name, name, {}, execute) == "Tool execution denied by policy"
    assert not executed


async def test_file_tools_refuse_symlinks_and_special_files(tmp_path):
    import os

    runtime = context(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-read-or-overwrite")
    (runtime.work / "link").symlink_to(outside)
    for execute in (
        lambda: runtime.read("link"),
        lambda: runtime.write("link", "bad"),
        lambda: runtime.edit("link", "must", "bad"),
    ):
        with pytest.raises(ValueError):
            await asyncio.to_thread(execute)
    fifo = runtime.work / "pipe"
    os.mkfifo(fifo)
    async with asyncio.timeout(2):
        for execute in (
            lambda: runtime.read("pipe"),
            lambda: runtime.write("pipe", "bad"),
            lambda: runtime.edit("pipe", "old", "new"),
        ):
            with pytest.raises(ValueError):
                await asyncio.to_thread(execute)
    assert outside.read_text() == "must-not-read-or-overwrite"


async def test_file_parent_swap_never_escapes_workspace(tmp_path):
    import threading

    runtime = context(tmp_path / "sandbox")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_text("outside-value")
    directory = runtime.work / "directory"
    directory.mkdir()
    (directory / "file").write_text("inside-value")
    moved = runtime.work / "moved"
    stop = threading.Event()

    def swap():
        while not stop.is_set():
            directory.rename(moved)
            directory.symlink_to(outside, target_is_directory=True)
            directory.unlink()
            moved.rename(directory)

    worker = threading.Thread(target=swap)
    worker.start()
    try:
        for _ in range(15):
            try:
                output = await asyncio.to_thread(runtime.read, "directory/file")
                assert output == "inside-value"
            except ValueError:
                pass
    finally:
        stop.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert (outside / "file").read_text() == "outside-value"


async def test_shell_uses_private_home_cwd_and_temporary_directory(tmp_path):
    import json

    runtime = context(tmp_path)
    result = await runtime.bash(
        "python3 -c 'import json,os,tempfile; "
        "f=tempfile.NamedTemporaryFile(); "
        'print(json.dumps([os.environ["HOME"],os.getcwd(),f.name]))\''
    )
    assert result.startswith("exit_code=0\n")
    home, cwd, temporary = json.loads(result.split("\n", 1)[1])
    assert home == cwd == str(runtime.work)
    assert Path(temporary).parent == runtime.home / "tmp"
    assert (runtime.home / "tmp").stat().st_mode & 0o777 == 0o700
