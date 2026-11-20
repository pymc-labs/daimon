"""An enclosed pod on a real Docker host, against a served world (CI's eval-sandbox-image job)."""

from __future__ import annotations

import hashlib
import os
import subprocess
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
    simulator = ScriptedSimulator({"status": 200, "body": {"answer": "simulated"}})
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
            assert "simulated" in (await run("curl -sS https://unknown.example.org/x"))[1]
            cloned = await run("git clone -q https://github.com/acme/app /tmp/a && cat /tmp/a/*")
            assert cloned == (0, "hello\n"), f"the case repository clones: {cloned}"
            raw = await run("timeout 10 bash -c 'echo > /dev/tcp/192.0.2.1/22'")
            assert raw[0] != 0, "a raw TCP connection is refused"
            assert (await run("id -u"))[1].strip() == "10001", "commands run as the agent"
        except AssertionError as exc:
            probe = (
                "cat /etc/resolv.conf; iptables-save -c; iptables-legacy-save -c; cat /proc/net/udp"
            )
            net = subprocess.run(["docker", "exec", "-u", "0", f"{pod.id}-net", "sh", "-c", probe],
                                 capture_output=True, text=True)  # fmt: skip
            raise AssertionError(
                f"{exc}\n{await runtime.logs(pod.id)}\n{net.stdout}{net.stderr}"
            ) from exc
        finally:
            await runtime.stop(pod)
