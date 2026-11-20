"""The Anthropic self-hosted environment worker for an eval pod (`apps/eval-sandbox`).

It runs as the executor uid with the environment key, and is not dumpable, so nothing it
starts can read the key from its memory or environment. Every tool runs as the agent uid
with the agent environment only: bash through the agent wrapper, and each file tool in a
short child started the same way, so a link the agent planted opens with its rights alone.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Any, Final

import anyio
from anthropic import AsyncAnthropic
from anthropic.lib.tools import BetaAsyncFunctionTool, BetaFunctionToolResultType, ToolError
from anthropic.lib.tools.agent_toolset import (
    AgentToolContext,
    BashSession,
    beta_agent_toolset_20260401,
)

AGENT_BASH: Final = "/opt/eval/agent-bin/bash"
_TOOL_CHILD: Final = 'exec /opt/venv/bin/python -I -m mux.drivers.anthropic.environment_worker "$@"'
_PR_SET_DUMPABLE: Final = 4


async def _agent_bash(
    cls: type[BashSession], workdir: str | os.PathLike[str], *, env: Mapping[str, str] | None = None
) -> BashSession:
    del env  # the wrapper builds the agent environment itself
    proc = await anyio.open_process(
        [AGENT_BASH, "--noprofile", "--norc"], cwd=workdir, env={"TERM": "dumb"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    return cls(proc)


class AgentFileTool(BetaAsyncFunctionTool[Any]):
    """A file tool that runs as the agent, in a child that does this one call."""

    def __init__(self, tool: BetaAsyncFunctionTool[Any], workdir: str) -> None:
        super().__init__(
            tool.func, name=tool.name, description=tool.description, input_schema=tool.input_schema
        )
        self.workdir = workdir

    async def call(self, input: object) -> BetaFunctionToolResultType:
        argv = [AGENT_BASH, "-c", _TOOL_CHILD, "tool", "tool", self.name, self.workdir]
        done = await anyio.run_process(argv, input=json.dumps(input).encode(), check=False, env={})
        try:
            reply = json.loads(done.stdout)
        except ValueError:
            raise ToolError(f"{self.name}: {done.stderr.decode(errors='replace')[-500:]}") from None
        if "error" in reply:
            raise ToolError(reply["error"])
        return reply["result"]


def agent_tools(ctx: AgentToolContext) -> list[BetaAsyncFunctionTool[Any]]:
    bash, *files = beta_agent_toolset_20260401(ctx)
    return [bash, *(AgentFileTool(tool, str(ctx.workdir)) for tool in files)]


def _tool_child(name: str, workdir: str) -> None:
    """One file tool call as the agent: the input on stdin, the result as JSON on stdout."""
    tools = {tool.name: tool for tool in beta_agent_toolset_20260401(AgentToolContext(workdir))}
    try:
        reply: dict[str, object] = {"result": asyncio.run(tools[name].call(json.load(sys.stdin)))}
    except ToolError as error:
        reply = {"error": error.content}
    except (ValueError, TypeError) as error:
        reply = {"error": f"{name}: {error}"}
    print(json.dumps(reply))


def install() -> None:
    """Start bash through the agent wrapper; make this process undumpable."""
    setattr(BashSession, "start", classmethod(_agent_bash))  # noqa: B010 - replaces the SDK's /bin/bash spawn
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot make the environment worker undumpable")


async def run() -> None:
    install()
    environment_id = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
    environment_key = os.environ.pop("ANTHROPIC_ENVIRONMENT_KEY")
    client = AsyncAnthropic(api_key="unused")
    worker = client.beta.environments.work.worker(
        environment_id=environment_id,
        environment_key=environment_key,
        tools=agent_tools,
        workdir="/workspace",
    )
    await worker.run()


if __name__ == "__main__" and sys.argv[1:2] == ["tool"]:
    _tool_child(sys.argv[2], sys.argv[3])
