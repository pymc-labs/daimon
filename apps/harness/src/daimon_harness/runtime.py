"""Turn context, workspace tools, and mounted Agent Skills."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import uuid4

from mux.drivers.selfhosted.wire import (
    TURN_SKILLS_MAX_BYTES,
    ActionRequest,
    EventType,
    InlineSkill,
    TurnRequest,
)
from pydantic import JsonValue

from .credentials import redact
from .errors import HarnessError
from .isolation import can_switch_uid, tool_identity
from .journal import identifier


async def discard_event(kind: EventType, data: dict[str, JsonValue]) -> None:
    """No-op event sink for standalone workspace probes."""


@dataclass
class TurnContext:
    request: TurnRequest
    home: Path
    emit: Callable[[EventType, dict[str, JsonValue]], Awaitable[None]]
    pending: dict[str, asyncio.Future[ActionRequest]] = field(
        default_factory=dict[str, asyncio.Future[ActionRequest]]
    )
    decisions: dict[str, ActionRequest] = field(default_factory=dict[str, ActionRequest])
    tool_calls: int = 0
    credentials: dict[str, str] = field(default_factory=dict[str, str], repr=False)

    credential_bindings: dict[str, str] | None = None
    tool_uid: int = 10001
    state_dir: Path | None = None
    model_input: list[JsonValue] = field(default_factory=list[JsonValue], repr=False)
    process_groups: set[int] = field(default_factory=set[int], repr=False)

    def __post_init__(self) -> None:
        tool_identity(self.home, self.tool_uid)

    def permission(self, name: str) -> Literal["auto", "ask", "deny"]:
        exact = self.request.tool_policy.get(name)
        if exact is not None:
            return exact
        if "/" in name:
            return self.request.tool_policy.get(name.split("/", 1)[0] + "/*", "auto")
        return "auto"

    async def tool(
        self,
        call_id: str,
        name: str,
        arguments: dict[str, JsonValue],
        execute: Callable[[], Awaitable[JsonValue]],
        *,
        mcp: bool = False,
    ) -> JsonValue:
        """Observe and authorize tools before any side effect, including MCP calls."""
        self.tool_calls += 1
        result_sent = False

        async def result(output: JsonValue, is_error: bool = False) -> JsonValue:
            nonlocal result_sent
            result_sent = True
            output = redact(output, self.credentials)
            await self.emit(
                "tool_result", {"call_id": call_id, "output": output, "is_error": is_error}
            )
            return output

        await self.emit(
            "tool_use",
            {
                "call_id": call_id,
                "name": name,
                "input": arguments,
                "executor": "mcp" if mcp else "agent",
            },
        )
        if self.permission(name) == "deny":
            return await result("Tool execution denied by policy", True)
        if self.tool_calls > self.request.limits.max_tool_calls:
            await result("Maximum tool calls exceeded", True)
            raise HarnessError("tool_limit", "Maximum tool calls exceeded")
        try:
            if self.permission(name) == "ask":
                action_id = str(uuid4())
                future: asyncio.Future[ActionRequest] = asyncio.get_running_loop().create_future()
                self.pending[action_id] = future
                try:
                    await self.emit(
                        "requires_action",
                        {
                            "action_id": action_id,
                            "kind": "tool_confirmation",
                            "call_id": call_id,
                            "name": name,
                            "input": arguments,
                        },
                    )
                    decision = await future
                finally:
                    self.pending.pop(action_id, None)
                if decision.decision == "deny":
                    return await result("Tool execution denied", True)
                if decision.decision == "result":
                    return await result(decision.result)
            output = await execute()
        except asyncio.CancelledError:
            if not result_sent:
                await result("Tool interrupted; side effects may have occurred", True)
            raise
        except Exception as exc:
            return await result(f"Tool failed: {type(exc).__name__}", True)
        return await result(output)

    @property
    def work(self) -> Path:
        path = self.home / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, name: str) -> Path:
        path = (self.work / name).resolve()
        if not path.is_relative_to(self.work.resolve()):
            raise ValueError("path is outside workspace")
        return path

    async def bash(self, command: str) -> str:
        uid, gid, groups = tool_identity(self.home, self.tool_uid)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(Path(__file__).with_name("sandbox_exec.py")),
            str(max(1, min(300, math.ceil(self.request.limits.deadline_s)))),
            str(4 * 1024 * 1024 * 1024),
            "1024" if uid is not None else "16384",
            command,
            user=uid,
            group=gid,
            extra_groups=groups,
            cwd=self.home,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            # Model and harness credentials must not be visible in child processes.
            env={
                "HOME": str(self.work),
                "PYTHONUSERBASE": str(self.work / ".local"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
            },
        )
        self.process_groups.add(process.pid)
        try:
            async with asyncio.timeout(min(300, self.request.limits.deadline_s)):
                assert process.stdout is not None
                tail = bytearray()
                truncated = False
                while chunk := await process.stdout.read(65_536):
                    tail.extend(chunk)
                    if len(tail) > 262_144:
                        truncated = True
                        del tail[:-262_144]
                await process.wait()
                marker = "[output truncated]\n" if truncated else ""
                return f"exit_code={process.returncode}\n{marker}{tail.decode(errors='replace')}"
        finally:
            # Also reap detached background jobs after a successful command.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()
            self.process_groups.discard(process.pid)

    def stop_processes(self) -> None:
        for pid in self.process_groups:
            with suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
        if can_switch_uid():
            # A child can start a new session and leave its original process group.
            # The session-specific UID also seals those descendants at turn end.
            for entry in Path("/proc").iterdir():
                if entry.name.isdigit():
                    with suppress(ProcessLookupError, FileNotFoundError):
                        if entry.stat().st_uid == self.tool_uid:
                            os.kill(int(entry.name), signal.SIGKILL)

    def file_operation(
        self, operation: str, path: str, *, content: str = "", old: str = "", new: str = ""
    ) -> str:
        uid, gid, groups = tool_identity(self.home, self.tool_uid)
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("file_exec.py")), str(self.home)],
            input=json.dumps(
                {"operation": operation, "path": path, "content": content, "old": old, "new": new}
            ),
            text=True,
            capture_output=True,
            timeout=5,
            user=uid,
            group=gid,
            extra_groups=groups,
            cwd=self.home,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.work), "LANG": "C.UTF-8"},
        )
        if result.returncode != 0:
            raise ValueError("File operation refused or failed")
        output: object = json.loads(result.stdout).get("output")
        if not isinstance(output, str):
            raise ValueError("Invalid file operation response")
        return output

    def read(self, path: str) -> str:
        return self.file_operation("read", path)

    def write(self, path: str, content: str) -> str:
        return self.file_operation("write", path, content=content)

    def edit(self, path: str, old: str, new: str) -> str:
        return self.file_operation("edit", path, old=old, new=new)

    async def skills(self) -> str:
        descriptions: list[str] = []
        root = self.home / "skills"
        if root.is_symlink():
            raise ValueError("skills root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        total = 0
        for skill in self.request.skills:
            if isinstance(skill, InlineSkill):
                # Recheck decoded caps/digest at the materialisation boundary.
                bundle = InlineSkill.model_validate(skill.model_dump())
            else:
                raise ValueError("URL skills are not supported; send an inline bundle")
            files = bundle.decoded_files()
            total += sum(map(len, files.values()))
            if total > TURN_SKILLS_MAX_BYTES:
                raise ValueError("turn skills exceed 24 MiB decoded bytes")
            if "SKILL.md" not in files:
                raise ValueError("skill requires SKILL.md")
            instructions = files["SKILL.md"].decode("utf-8")
            directory = root / identifier(bundle.name)
            if directory.is_symlink():
                raise ValueError("skill directory must not be a symlink")
            cache = (self.state_dir or self.home / ".daimon") / "skills"
            if cache.is_symlink() or cache.parent.is_symlink():
                raise ValueError("skill cache must not be a symlink")
            cache.mkdir(parents=True, exist_ok=True)
            marker = cache / (bundle.name + ".sha256")
            if marker.is_symlink():
                raise ValueError("skill cache marker must not be a symlink")
            stored = marker.read_text() if marker.exists() else None
            if stored != bundle.sha256 or not directory.is_dir():
                # Stage a complete bundle before replacing it; stale files disappear.
                staged = Path(tempfile.mkdtemp(prefix=".skill-", dir=root))
                staged.chmod(0o755)
                try:
                    for name, content in files.items():
                        path = staged / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                    if directory.exists():
                        shutil.rmtree(directory)
                    staged.rename(directory)
                    temporary_marker = marker.with_suffix(".tmp")
                    if temporary_marker.is_symlink():
                        raise ValueError("skill cache marker must not be a symlink")
                    temporary_marker.write_text(bundle.sha256)
                    temporary_marker.replace(marker)
                finally:
                    if staged.exists():
                        shutil.rmtree(staged)
            descriptions.append(f"Skill {bundle.name}: {directory / 'SKILL.md'}\n{instructions}")
        return "\n\n".join(descriptions)
