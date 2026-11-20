"""An enclosed pod on a real Docker host, against a served world (CI's eval-sandbox-image job)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from daimon.core.config import EvalsSettings
from daimon.core.evals.sandbox.docker import DockerRuntime, docker_client
from daimon.core.evals.sandbox.runtime import PodSpec
from daimon.core.evals.world.dispatch import FakeWorld, issue_world_token
from daimon.core.stores import evals
from daimon.core.stores.evals import BlobRow
from daimon.testing.eval_world import eval_case, make_running_item
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_sandbox import fixture_ca
from .test_world import ScriptedSimulator, repo_bundle, serve_world

IMAGE = os.environ.get("EVAL_SANDBOX_IMAGE", "")
pytestmark = pytest.mark.skipif(not IMAGE, reason="needs a built eval sandbox image")


def _bridge_gateway() -> str:
    found = subprocess.run(
        ["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return found.stdout.strip()


#: The executor's environment in a pod (`daimon_eval_sandbox.__main__.EXEC_ENV`).
_EXEC_ENV = {
    "PATH": "/opt/eval/agent-bin:/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
    "SHELL": "/opt/eval/agent-bin/bash",
    "HOME": "/home/exec",
}
_LISTENER = (
    "import socket\ns = socket.create_server(('0.0.0.0', 443))\n"
    "while True:\n    c, _ = s.accept(); c.sendall(b'REAL-WORLD'); c.close()\n"
)
_RAW_TCP = (
    "import socket, sys\nc = socket.create_connection((sys.argv[1], 443), timeout=10)\n"
    "c.sendall(b'\\x00\\x01not-http')\nprint(c.recv(64))\n"
)


@contextmanager
def _real_host() -> Iterator[str]:
    """A host on the Docker bridge that answers raw TCP on 443, as the real world would."""
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--entrypoint", "/usr/local/bin/python3", IMAGE,
         "-c", _LISTENER],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    container = started.stdout.strip()
    try:
        found = subprocess.run(
            ["docker", "inspect", container, "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        yield found.stdout.strip()
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


def _as_executor(pod: str, *argv: str) -> str:
    env = [arg for name, value in _EXEC_ENV.items() for arg in ("-e", f"{name}={value}")]
    done = subprocess.run(
        ["docker", "exec", "-u", "exec", "-w", "/workspace", *env, f"{pod}-main", *argv],
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    return done.stdout + done.stderr


async def test_pod_reaches_only_the_world(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    content = repo_bundle(tmp_path)
    tenant = await make_tenant(db_session, platform="web")
    sha = hashlib.sha256(content).hexdigest()
    blob = BlobRow(sha256=sha, size=len(content), media_type="application/x-git-bundle",
                   scrub="clean", needs_human_check=False)  # fmt: skip
    await evals.put_blob(db_session, tenant_id=tenant.id, blob=blob, content=content)
    ping = {"http": {"method": "GET", "url": "https://api.example.com/v1/ping"}}
    case = eval_case({
        "repos": [{"url": "https://github.com/acme/app", "default_branch": "main",
                   "bundle": {"sha256": sha, "size": len(content), "media_type": blob.media_type}}],
        "exchanges": [{"id": "x1", "surface": "http", "request": ping,
                       "response": {"http": {"status": 200, "body": {"json": {"ok": "recorded"}}}}}],
    })  # fmt: skip
    item_id = await make_running_item(db_session, tenant.id, case)
    token = await issue_world_token(db_session, item_id)
    simulator = ScriptedSimulator({"status": 200, "body": {"answer": "invented"}})
    world = FakeWorld(db_session_factory, simulator, tmp_path / "world")
    item = await world.item(item_id)
    assert item is not None, "the item runs"
    runtime = DockerRuntime(docker_client(EvalsSettings()))
    # On all interfaces: the pod reaches it over the Docker bridge.
    async with serve_world(world, host="0.0.0.0") as base:
        url = base.replace("127.0.0.1", _bridge_gateway())
        spec = PodSpec(item, IMAGE, "probe", url, token, fixture_ca())
        pod = await runtime.start(spec)
        try:

            async def run(command: str) -> tuple[int, str]:
                done = await runtime.run(pod, command, timeout_s=60)
                return done.exit_code, (done.stdout + done.stderr).decode()

            assert "recorded" in (await run("curl -sS https://api.example.com/v1/ping"))[1]
            assert "invented" in (await run("curl -sS https://unknown.example.org/x"))[1]
            cloned = await run("git clone -q https://github.com/acme/app /tmp/a && cat /tmp/a/*")
            assert cloned == (0, "hello\n"), f"the case repository clones: {cloned}"
            raw = await run("timeout 10 bash -c 'echo > /dev/tcp/192.0.2.1/22'")
            assert raw[0] != 0, "a raw TCP connection is refused"
            with _real_host() as host:
                relayed = await run(f"python3 -c {shlex.quote(_RAW_TCP)} {host}")
            assert "REAL-WORLD" not in relayed[1], f"raw TCP on 443 never leaves: {relayed}"
            assert (await run("id -u"))[1].strip() == "10001", "commands run as the agent"
            await run("ln -s /run/eval/main /workspace/out")
            leaked = await runtime.files(pod, "/workspace/out")
            assert not leaked, f"files are read with the agent's rights: {leaked}"
            verdicts = await asyncio.to_thread(
                _as_executor, pod.id, "/opt/venv/bin/python", "-m", "daimon_eval_sandbox",
                "probe", "isolation",
            )  # fmt: skip
            failed = {k: v for k, v in json.loads(verdicts).items() if v != "pass"}
            assert not failed, f"every executor command path runs as the agent: {verdicts}"
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n{await runtime.logs(pod.id)}") from exc
        finally:
            await runtime.stop(pod)
