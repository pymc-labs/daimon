"""Turn context, workspace tools, and mounted Agent Skills."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
from mux.drivers.selfhosted.wire import ActionRequest, EventType, InlineSkill, TurnRequest
from pydantic import JsonValue

from .journal import identifier


@dataclass
class TurnContext:
    request: TurnRequest
    home: Path
    emit: Callable[[EventType, dict[str, JsonValue]], None]
    pending: dict[str, asyncio.Future[ActionRequest]] = field(
        default_factory=dict[str, asyncio.Future[ActionRequest]]
    )
    decisions: dict[str, ActionRequest] = field(default_factory=dict[str, ActionRequest])
    tool_calls: int = 0

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
        if self.tool_calls > self.request.limits.max_tool_calls:
            raise RuntimeError("maximum tool calls exceeded")
        self.emit(
            "tool_use",
            {
                "call_id": call_id,
                "name": name,
                "input": arguments,
                "executor": "mcp" if mcp else "agent",
            },
        )
        confirmation = os.environ.get("HARNESS_CONFIRM_TOOLS", "bash,write,edit").split(",")
        if mcp or name in confirmation or "*" in confirmation:
            action_id = str(uuid4())
            future: asyncio.Future[ActionRequest] = asyncio.get_running_loop().create_future()
            self.pending[action_id] = future
            self.emit(
                "requires_action",
                {
                    "action_id": action_id,
                    "kind": "tool_confirmation",
                    "call_id": call_id,
                    "name": name,
                    "input": arguments,
                },
            )
            try:
                decision = await future
            finally:
                self.pending.pop(action_id, None)
            if decision.decision == "deny":
                output: JsonValue = "Tool execution denied"
                self.emit("tool_result", {"call_id": call_id, "output": output, "is_error": True})
                return output
            if "result" in decision.model_fields_set:
                self.emit(
                    "tool_result",
                    {"call_id": call_id, "output": decision.result, "is_error": False},
                )
                return decision.result
        try:
            output = await execute()
            self.emit("tool_result", {"call_id": call_id, "output": output, "is_error": False})
            return output
        except (OSError, ValueError) as exc:
            output = f"Tool failed: {type(exc).__name__}"
            self.emit("tool_result", {"call_id": call_id, "output": output, "is_error": True})
            return output

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
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-lc",
            command,
            cwd=self.work,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            # Model and harness credentials must not be visible in child processes.
            env={
                "HOME": str(self.home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
            },
        )
        try:
            output, _ = await process.communicate()
            return f"exit_code={process.returncode}\n{output.decode(errors='replace')[-100_000:]}"
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    def read(self, path: str) -> str:
        target = (self.work / path).resolve()
        if not any(
            target.is_relative_to(root.resolve()) for root in (self.work, self.home / "skills")
        ):
            raise ValueError("path is outside workspace and skills")
        with target.open() as source:
            return source.read(100_000)

    def write(self, path: str, content: str) -> str:
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return "written"

    def edit(self, path: str, old: str, new: str) -> str:
        target = self.path(path)
        content = target.read_text()
        if not old or content.count(old) != 1:
            raise ValueError("old text must occur exactly once")
        target.write_text(content.replace(old, new, 1))
        return "edited"

    async def skills(self) -> str:
        descriptions: list[str] = []
        for skill in self.request.skills:
            directory = self.home / "skills" / identifier(skill.name)
            directory.mkdir(parents=True, exist_ok=True)
            if isinstance(skill, InlineSkill):
                files = skill.files
            else:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(skill.url)
                    response.raise_for_status()
                    files = {"SKILL.md": response.text}
            if "SKILL.md" not in files:
                raise ValueError("skill requires SKILL.md")
            for name, content in files.items():
                path = (directory / name).resolve()
                if not path.is_relative_to(directory.resolve()):
                    raise ValueError("skill file is outside skill directory")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            descriptions.append(
                f"Skill {skill.name}: {directory / 'SKILL.md'}\n{files['SKILL.md']}"
            )
        return "\n\n".join(descriptions)
