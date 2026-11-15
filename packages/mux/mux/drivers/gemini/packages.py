"""Environment packages, installed in the background by a hook that gates sandbox tools.

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
# A sandbox tool call fails at about 300 seconds, hook time included, so a call
# waits at most WAIT_SECONDS while the install runs on in the background. Hooks
# count a timeout as allow, so the hook's own limit is only a backstop.
WAIT_SECONDS = 90
TIMEOUT_SECONDS = WAIT_SECONDS + 30
ATTEMPTS = 3
# A hung install is stopped (where `timeout` exists) and counts as a failed attempt.
INSTALL_SECONDS = 1200

_SCRIPT = """\
#!/bin/sh
# Installs the environment's packages once, in a detached worker (`sh $0 run`).
cat >/dev/null
m="${TMPDIR:-/tmp}/.daimon-packages-@digest@"
l="$m.pid"
if [ "$1" = run ]; then
  echo $$ >"$l"
  s=; [ "$(id -u)" = 0 ] || s="sudo -n"
  if { @install@; } >"$m.log" 2>&1; then touch "$m"; else echo >>"$m.failed"; fi
  [ "$(cat "$l")" = $$ ] && rm -f "$l"
  exit 0
fi
allow() { echo '{"decision":"allow"}'; exit 0; }
deny() { printf '{"decision":"deny","reason":"%s"}\\n' "$1"; exit 0; }
d=; command -v setsid >/dev/null 2>&1 && d=setsid
t=; command -v timeout >/dev/null 2>&1 && t="timeout @limit@"
i=0
until [ -e "$m" ]; do
  p=$(cat "$l" 2>/dev/null)
  if [ -e "$l" ]; then
    # A killed install (or hook) leaves its pid behind; reclaim it so it retries.
    if [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null && [ ! -e "$m" ] &&
      mv "$l" "$l.$$" 2>/dev/null; then
      rm -f "$l.$$"; echo >>"$m.failed"; continue
    fi
  elif [ "$(cat "$m.failed" 2>/dev/null | wc -l)" -ge @attempts@ ]; then
    mkdir "$m.told" 2>/dev/null || allow
    r=$(tail -c 300 "$m.log" 2>/dev/null | tr -c 'A-Za-z0-9._:/=+-' ' ' | tr -s ' ')
    deny "Installing the environment packages failed:$r (log $m.log). Tools now run without them."
  elif (set -C; echo $$ >"$l") 2>/dev/null; then
    $d $t sh "$0" run </dev/null >/dev/null 2>&1 &
  fi
  [ "$i" -ge @wait@ ] && deny "The environment packages are still installing; retry in a minute."
  sleep 1; i=$((i+1))
done
allow
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
    script = _SCRIPT
    for key, value in (
        ("digest", digest),
        ("wait", WAIT_SECONDS),
        ("limit", INSTALL_SECONDS),
        ("attempts", ATTEMPTS),
        ("install", install),
    ):
        script = script.replace(f"@{key}@", str(value))
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
