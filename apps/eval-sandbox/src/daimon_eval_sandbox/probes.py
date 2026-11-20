"""Enclosure probes, run as the agent inside a pod: does each tool reach the world over TLS?

A probe passes when the tool got past name lookup and the TLS handshake with the eval CA,
whatever the world then answered. A tool missing from the image is `skipped`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
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
    "anthropic": ["/opt/venv/bin/python", "-c", "import anthropic.lib.environments"],
    "harness": ["/opt/venv/bin/python", "-c", "import daimon_harness"],
}
Verdict = Literal["pass", "fail", "skipped"]


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


def run_probes(names: list[str]) -> dict[str, Verdict]:
    every = {**NETWORK, **EXECUTORS}
    return {name: probe(every[name], network=name in NETWORK) for name in (names or list(every))}
