"""Where a reply's sandbox holds the agent's keys and the files sent with the reply.

Claude mounts both under /mnt/session; OpenAI and Gemini sandboxes have only
/workspace. The guidance names both, and the default skills' `workflow_io.py`
tries them in this order (`KEY_FILES`, `OUTPUT_DIRS`).
"""

from __future__ import annotations

from typing import NamedTuple


class SandboxPaths(NamedTuple):
    keys: str
    outputs: str


CLAUDE = SandboxPaths(keys="/mnt/session/uploads/.env", outputs="/mnt/session/outputs")
WORKSPACE = SandboxPaths(keys="/workspace/.daimon/.env", outputs="/workspace/outputs")
