"""Check a built image: the real control-plane caller against a running container.

    docker run --rm -d -p 127.0.0.1:18080:8080 -e MCP_AUTH_TOKEN=smoke compute-session
    uv run python apps/compute-session/tests/image_smoke.py http://127.0.0.1:18080 smoke

Add ``--pymc`` to install PyMC in the container and sample a model as a background
command, which needs the network and takes about a minute. Not collected by pytest.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from daimon.core import compute_tasks as scripts
from daimon.core.compute_fly import FlyComputeProvider
from daimon.core.compute_models import ComputeSession
from daimon.core.compute_provider import app_name
from daimon.core.config import ComputeSettings
from pydantic import JsonValue, SecretStr

DEPLOYMENT = "image-smoke"
#: What docs/compute.md and the task scripts need on the machine.
TOOLS = "bash sh setsid timeout base64 tail nohup python3 uv git gcc g++ make"
PYMC = (
    "set -e; cd /tmp; uv venv -q pymc-env; "
    "uv pip install -q --python pymc-env/bin/python pymc; "
    "pymc-env/bin/python -c 'import pymc as pm\n"
    'with pm.Model():\n    pm.Normal("x")\n'
    '    print("draws", pm.sample(50, tune=50, chains=1, progressbar=False).posterior.x.size)\''
)


#: What a command might try against the server. $PPID is the server: it starts the shell.
ATTACKS = (
    "kill -TERM 1",
    "kill -KILL 1",
    "kill -TERM $PPID",
    "kill -KILL $PPID",
    "pkill -KILL -f compute_session",
    "pkill -KILL -u server,root",
    "pkill -KILL -u work",
    "kill -9 -1",
    "head -c 64 /proc/$PPID/environ /proc/1/environ",
    "echo x > /proc/$PPID/mem",
)
#: The server and process 1 with their start times; a restart of either changes it.
IDENTITY = "ps -o pid=,lstart=,ruser=,euser=,suser= -p 1,$PPID"


class ToContainer(httpx.AsyncBaseTransport):
    """Sends the caller's requests for ``https://<app>.fly.dev`` to the container."""

    def __init__(self, base: str) -> None:
        self.base = httpx.URL(base)
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme=self.base.scheme, host=self.base.host, port=self.base.port
        )
        request.headers["host"] = self.base.netloc.decode()
        return await self.inner.handle_async_request(request)


class Machine:
    def __init__(self, base: str, token: str) -> None:
        settings = ComputeSettings(
            deployment_id=DEPLOYMENT,
            org_slug="org",
            api_token=SecretStr("unused"),
            session_auth_key=SecretStr("unused"),
        )
        session_id = uuid.uuid4()
        self.provider = FlyComputeProvider(settings, transport=ToContainer(base))
        self.token = token
        self.session = ComputeSession(
            id=session_id,
            tenant_id=uuid.uuid4(),
            owner_account_id=uuid.uuid4(),
            owner_external_user_id="smoke",
            deployment_id=DEPLOYMENT,
            platform="discord",
            parent_channel_id="parent",
            thread_id="thread",
            agent_id="agent",
            agent_name="smoke",
            request_key="request",
            app_name=app_name(DEPLOYMENT, session_id),
            profile="work",
            image="compute-session",
            region="iad",
            cpu_kind="shared",
            cpus=2,
            memory_mb=4096,
            ttl_hours=1,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            created_at=datetime.now(UTC),
        )

    async def call(self, tool: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        result = await self.provider.call(self.session, self.token, tool, arguments)
        assert isinstance(result, dict), result
        return result

    async def bash(self, command: str, timeout_s: int = 40) -> str:
        result = await self.call("run_bash", {"command": command, "timeout_s": timeout_s})
        assert result["exit_code"] == 0 and isinstance(result["stdout"], str), result
        return result["stdout"]

    async def survives_its_commands(self) -> None:
        """Nothing a command can do as ``work`` stops the server or shows it the token."""
        before = await self.bash(IDENTITY)
        print(before, end="")
        assert before.split()[-3:] == ["server", "work", "server"], (
            "server and commands share a user"
        )
        for attack in ATTACKS:
            result = await self.call("run_bash", {"command": attack + " 2>&1", "timeout_s": 20})
            assert isinstance(result["stdout"], str)
            print(f"$ {attack}\n  exit {result['exit_code']}: {result['stdout'].strip()[:150]}")
            assert self.token not in result["stdout"]
            assert await self.bash(IDENTITY) == before, f"the server did not survive: {attack}"
        print("server and process 1 unchanged after every attempt")

    async def background(self, command: str, *, wait_s: float, kill: bool = False) -> scripts.Probe:
        """Start a background command the way the control plane does and wait for its end."""
        task_id = uuid.uuid4()
        start = scripts.start_script(task_id, command, timeout_s=600)
        scripts.parse_start(await self.call("run_bash", {"command": start, "timeout_s": 30}))
        probe_args: dict[str, JsonValue] = {
            "command": scripts.probe_script(task_id),
            "timeout_s": 20,
        }
        deadline = time.monotonic() + wait_s
        killed = False
        while True:
            probe = scripts.parse_probe(await self.call("run_bash", probe_args))
            if probe.status == "exit":
                return probe
            assert probe.status in {"running", "missing"}, probe
            assert time.monotonic() < deadline, f"still {probe.status}: {probe.tail[-500:]}"
            if kill and probe.status == "running" and not killed:
                await self.call(
                    "run_bash", {"command": scripts.kill_script(task_id), "timeout_s": 20}
                )
                killed = True
            await asyncio.sleep(0.5)


async def check(base: str, token: str, *, pymc: bool) -> None:
    async with httpx.AsyncClient() as client:
        health = await client.get(base + "/health")
        assert (health.status_code, health.text) == (200, "ok"), health
        assert (await client.post(base + "/mcp/", json={})).status_code == 401
    machine = Machine(base, token)

    print(await machine.bash("id; pwd; python3 --version; uv --version; cat /proc/1/comm"))
    ids = await machine.bash("grep -E '^(Uid|Gid|Groups):' /proc/self/status")
    assert ids.split() == ["Uid:", *["10000"] * 4, "Gid:", *["10000"] * 4, "Groups:"], ids
    assert (await machine.bash("echo ${MCP_AUTH_TOKEN-unset}")).strip() == "unset"
    await machine.bash(f"for tool in {TOOLS}; do command -v $tool >/dev/null || exit 1; done")
    await machine.bash("touch /tmp/probe ~/probe && rm /tmp/probe ~/probe")
    await machine.bash(
        "echo '#include <Python.h>' | "
        "g++ -fsyntax-only -x c++ -I\"$(python3 -c 'import sysconfig; "
        'print(sysconfig.get_paths()["include"])\')" -'
    )
    assert await machine.call("write_file", {"path": "/home/work/a/b.txt", "content": "hi"}) == {
        "path": "/home/work/a/b.txt",
        "bytes": 2,
    }
    read = await machine.call("read_file", {"path": "/home/work/a/b.txt", "offset": 0, "limit": 9})
    assert (read["content"], read["eof"]) == ("hi", True), read
    listed = await machine.call("list_files", {"path": "/home/work/a", "pattern": "*"})
    assert listed["entries"] == [{"name": "b.txt", "is_dir": False, "size": 2}], listed
    # The file tools act as the session owner, never as the server's own user or root.
    assert (await machine.bash("stat -c %U:%G /home/work/a /home/work/a/b.txt")).split() == [
        "work:work",
        "work:work",
    ]
    for path in ("/usr/local/denied.txt", "/opt/compute-session/denied.txt"):
        denied = await machine.call("write_file", {"path": path, "content": "x"})
        assert denied == {"error": f"Permission denied: {path}"}, denied
    cut = await machine.call("run_bash", {"command": "kill -TERM $$", "timeout_s": 5})
    assert cut["exit_code"] == 143, cut
    # The server, under its own user, must still be able to end a command of work.
    late = await machine.call("run_bash", {"command": "sleep 300", "timeout_s": 1})
    assert (late["exit_code"], late["timed_out"]) == (137, True), late
    assert (await machine.bash("pgrep -x sleep | wc -l")).strip() == "0"

    await machine.survives_its_commands()

    # An ended command that nothing reaps would be reported as running until its deadline.
    ended = await machine.background("echo hi; sleep 1; exit 3", wait_s=20)
    assert (ended.exit_code, ended.tail) == (3, "hi\n"), ended
    stopped = await machine.background("sleep 300", wait_s=20, kill=True)
    print("background: exit 3 seen; killed command ended with exit", stopped.exit_code)

    if pymc:
        started = time.monotonic()
        sampled = await machine.background(PYMC, wait_s=540)
        assert sampled.exit_code == 0 and "draws 50" in sampled.tail, sampled
        print(f"pymc: installed and sampled in {time.monotonic() - started:.0f} s")
    print("image smoke: ok")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", help="for example http://127.0.0.1:18080")
    parser.add_argument("token", help="the MCP_AUTH_TOKEN the container was started with")
    parser.add_argument("--pymc", action="store_true", help="also install PyMC and sample a model")
    arguments = parser.parse_args()
    asyncio.run(check(arguments.base.rstrip("/"), arguments.token, pymc=arguments.pymc))


if __name__ == "__main__":
    main()
