"""Enclosure probes inside a pod: does each tool reach the world over TLS, and does every
command the executor starts run as the agent?

A network probe passes when the tool got past name lookup and the TLS handshake with the
eval CA, whatever the world then answered. A tool missing from the image is `skipped`.
`isolation` runs as the executor uid, through each path its executors start commands by.
"""

from __future__ import annotations

import asyncio
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final, Literal

URL: Final = "https://example.com/eval-probe"
_BROKEN: Final = re.compile(
    r"certificate|ssl|tls|could not resolve|name resolution|getaddrinfo|ENOTFOUND", re.I
)
_FETCH_PY: Final = (
    "import urllib.request as u, urllib.error as e\n"
    f"try: u.urlopen('{URL}', timeout=60)\nexcept e.HTTPError: pass\n"
)
#: Network probes pass on any answer; executor probes must exit cleanly.
NETWORK: Final[dict[str, list[str]]] = {
    "curl": ["curl", "-sS", "-o", "/dev/null", URL],
    "git": ["git", "ls-remote", "https://example.com/eval-probe.git"],
    "gh": ["gh", "api", "--hostname", "github.com", "/zen"],
    "python": ["python3", "-c", _FETCH_PY],
    "node": [
        "node",
        "-e",
        f"fetch('{URL}').then(() => 0, (e) => {{ console.error(e.cause ?? e); process.exit(1) }})",
    ],
    "gcloud": ["gcloud", "--quiet", "storage", "ls", "gs://eval-probe"],
}
EXECUTORS: Final[dict[str, list[str]]] = {
    "codex": ["codex", "--version"],
    "gemini-api": ["gemini-api", "version"],
    "anthropic": ["/opt/venv/bin/python", "-c", "import mux.drivers.anthropic.environment_worker"],
    "harness": ["/opt/venv/bin/python", "-c", "import daimon_harness"],
}
Verdict = Literal["pass", "fail", "skipped"]
#: How the executors start commands: `bash` and `sh` by PATH (gemini-api, the SDKs), by
#: path, as a login shell, through env (codex runs the argv it is sent), and below as
#: $SHELL and the passwd shell.
_SHELLS: Final = [
    ["bash", "-c"],
    ["sh", "-c"],
    ["/bin/sh", "-c"],
    ["/bin/bash", "-c"],
    ["/usr/bin/bash", "-lc"],
    ["/usr/bin/dash", "-c"],
    ["/usr/bin/env", "bash", "-c"],
]
_AGENT_UID: Final = "10001"


def probe(argv: list[str], *, network: bool) -> Verdict:
    if shutil.which(argv[0]) is None:
        return "skipped"
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "fail"
    if done.returncode == 0:
        return "pass"
    return "pass" if network and not _BROKEN.search(done.stdout + done.stderr) else "fail"


def _verdict(ok: bool) -> Verdict:
    return "pass" if ok else "fail"


def _uid_via(shell: list[str]) -> str:
    done = subprocess.run([*shell, "id -u"], capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


async def _anthropic_tools() -> tuple[str, str, str]:
    """The real Anthropic toolset as the worker installs it: bash's uid, and what `read`
    returns for a workspace file only the executor may read (it runs as the agent, so no
    link the agent swaps in can reach past the agent's own rights)."""
    from anthropic.lib.tools import ToolError
    from anthropic.lib.tools.agent_toolset import AgentToolContext
    from mux.drivers.anthropic.environment_worker import agent_tools, install

    install()
    secret = Path("/workspace/eval-secret")
    secret.write_text("EXECUTOR-ONLY")
    secret.chmod(0o600)
    tools = {tool.name: tool for tool in agent_tools(AgentToolContext("/workspace"))}
    uid = str(await tools["bash"].call({"command": "id -u"})).strip()
    # A restart kills the shell, which the executor uid may do only through agent-exec.
    restarted = str(await tools["bash"].call({"restart": True}))
    try:
        read = str(await tools["read"].call({"file_path": str(secret)}))
    except ToolError as error:
        read = str(error.content)
    return uid, read, restarted


def isolation() -> dict[str, Verdict]:
    shells = [*_SHELLS, [os.environ["SHELL"], "-c"], [pwd.getpwuid(os.getuid()).pw_shell, "-c"]]
    verdicts: dict[str, Verdict] = {
        " ".join(shell): _verdict(_uid_via(shell) == _AGENT_UID) for shell in shells
    }
    uid, read, restarted = asyncio.run(_anthropic_tools())
    verdicts["anthropic bash"] = _verdict(uid == _AGENT_UID)
    verdicts["anthropic bash restart"] = _verdict(restarted == "bash session restarted")
    verdicts["anthropic read"] = _verdict("EXECUTOR-ONLY" not in read)
    # Unreadable binaries are undumpable: what they start cannot read their memory or env.
    for binary in ("codex", "gemini-api"):
        verdicts[f"{binary} unreadable"] = _verdict(
            not os.access(str(shutil.which(binary)), os.R_OK)
        )
    return verdicts


def run_probes(names: list[str]) -> dict[str, Verdict]:
    if names == ["isolation"]:
        return isolation()
    every = {**NETWORK, **EXECUTORS}
    return {name: probe(every[name], network=name in NETWORK) for name in (names or list(every))}
