"""Environment packages, installed by a sandbox hook before the first tool call.

Gemini environments have no package field (Environments API reference), but a
`pre_tool_execution` hook runs before every sandbox tool (Agent hooks guide), and
installs persist while the environment ID is reused (Agent environment guide).
"""

import hashlib
import json
import shlex
from collections.abc import Mapping

from pydantic import JsonValue

from mux.profiles import PACKAGE_MANAGERS

# Both paths are in the documented hook discovery list and outside the workspace
# snapshot, so neither reads as the agent's output.
HOOKS_TARGET = "/.agents/hooks.json"
SCRIPT_TARGET = "/.agents/hooks-scripts/daimon-packages.sh"
# Hooks default to 30 seconds and count a timeout as allow; installs take longer.
TIMEOUT_SECONDS = 600

_SCRIPT = """\
#!/bin/sh
# Installs the environment's packages once; later tool calls pass straight through.
cat >/dev/null
m="${{TMPDIR:-/tmp}}/.daimon-packages-{digest}"
allow() {{ echo '{{"decision":"allow"}}'; exit 0; }}
if [ -e "$m" ] || [ -e "$m.failed" ]; then allow; fi
if ! mkdir "$m.lock" 2>/dev/null; then
  i=0
  while [ ! -e "$m" ] && [ ! -e "$m.failed" ] && [ "$i" -lt {wait} ]; do sleep 1; i=$((i+1)); done
  allow
fi
s=; [ "$(id -u)" = 0 ] || s="sudo -n"
if {{ {install}; }} >"$m.log" 2>&1; then touch "$m"; allow; fi
touch "$m.failed"
echo '{{"decision":"deny","reason":"Installing the environment packages failed; see '"$m.log"'"}}'
"""


def _install(manager: str, names: tuple[str, ...]) -> str:
    args = " ".join(shlex.quote(name) for name in names)
    if manager == "system":
        return (
            f"$s apt-get update -qq && "
            f"$s env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {args}"
        )
    if manager == "python":
        # Debian-based images mark the system Python as externally managed.
        return (
            f"{{ python3 -m pip install -q {args} || "
            f"python3 -m pip install -q --break-system-packages {args}; }}"
        )
    return f"npm install -g --silent {args}"


def package_sources(packages: Mapping[str, tuple[str, ...]]) -> list[JsonValue]:
    """The inline hook and script that install `packages`, or none when there are none."""
    wanted = {manager: packages[manager] for manager in PACKAGE_MANAGERS if packages.get(manager)}
    if not wanted:
        return []
    digest = hashlib.sha256(json.dumps(wanted, sort_keys=True).encode()).hexdigest()[:16]
    install = " && ".join(_install(manager, names) for manager, names in wanted.items())
    script = _SCRIPT.format(digest=digest, wait=TIMEOUT_SECONDS - 10, install=install)
    hook = {"type": "command", "command": f"sh {SCRIPT_TARGET}", "timeout": TIMEOUT_SECONDS}
    hooks = {
        "daimon-packages": {
            "enabled": True,
            "pre_tool_execution": [{"matcher": ".*", "hooks": [hook]}],
        }
    }
    return [
        {"type": "inline", "target": HOOKS_TARGET, "content": json.dumps(hooks, sort_keys=True)},
        {"type": "inline", "target": SCRIPT_TARGET, "content": script},
    ]


def inline_bytes(environment: JsonValue) -> int:
    """The inline source bytes a compiled environment spends of the shared inline budget."""
    sources = environment.get("sources") if isinstance(environment, dict) else None
    total = 0
    for source in sources if isinstance(sources, list) else ():
        content = source.get("content") if isinstance(source, dict) else None
        if isinstance(source, dict) and source.get("type") == "inline" and isinstance(content, str):
            total += len(content.encode("utf-8"))
    return total
