"""The Anthropic self-hosted environment worker for an eval pod (`apps/eval-sandbox`).

It runs as the executor uid with the environment key in its own environment. Its bash
tool is started through the agent wrapper, so commands run as the agent uid with the
agent environment only. The file tools stay in-process, confined to the workspace.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from typing import Final

import anyio
from anthropic import AsyncAnthropic
from anthropic.lib.tools.agent_toolset import BashSession

AGENT_BASH: Final = "/opt/eval/agent-bin/bash"


async def _agent_bash(
    cls: type[BashSession], workdir: str | os.PathLike[str], *, env: Mapping[str, str] | None = None
) -> BashSession:
    del env  # the wrapper builds the agent environment itself
    proc = await anyio.open_process(
        [AGENT_BASH, "--noprofile", "--norc"], cwd=workdir, env={"TERM": "dumb"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    return cls(proc)


async def run() -> None:
    setattr(BashSession, "start", classmethod(_agent_bash))  # noqa: B010 - replaces the SDK's /bin/bash spawn
    environment_id = os.environ["ANTHROPIC_ENVIRONMENT_ID"]
    environment_key = os.environ.pop("ANTHROPIC_ENVIRONMENT_KEY")
    client = AsyncAnthropic(api_key="unused")
    worker = client.beta.environments.work.worker(
        environment_id=environment_id, environment_key=environment_key, workdir="/workspace"
    )
    await worker.run()
