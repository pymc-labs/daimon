"""Root/service/tool UID probe; run in an isolated subprocess, no live model."""

import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from daimon_harness.codex_adapter import CODEX_VERSION, CodexConnection, private_config
from daimon_harness.codex_relay import ResponsesRelay
from daimon_harness.isolation import prepare_service, protect_process
from daimon_harness.journal import Journal
from daimon_harness.runtime import TurnContext, discard_event
from mux.drivers.selfhosted.wire import TurnRequest


async def probe(home: Path) -> None:
    journal = Journal(home)
    journal.close()
    prepare_service(home)
    protect_process()
    assert os.geteuid() == 10000
    journal = Journal(home)
    sandbox, uid = journal.sandbox("probe")
    request = TurnRequest(
        session_id="probe",
        turn_id="probe",
        idempotency_key="probe",
        model="gpt-6-luna",
        instructions="",
        input=[],
        mcp_servers=[],
        skills=[],
        limits={"max_tool_calls": 1, "deadline_s": 15},
    )
    context = TurnContext(request, sandbox, discard_event, tool_uid=uid)

    async def headers():
        raise AssertionError("tool caused model credential acquisition")

    async with httpx.AsyncClient() as upstream:
        relay = ResponsesRelay(context, [], [], upstream, "https://fake.invalid", headers)
        with tempfile.TemporaryDirectory(dir=home / ".daimon") as scratch:
            directory = Path(scratch)
            async with relay.serve() as endpoint, AsyncExitStack() as cleanup:
                private_config(directory, endpoint, relay.capability, request.model)
                binary = shutil.which(os.environ.get("HARNESS_CODEX_BINARY", "codex"))
                assert binary is not None, "pinned Codex binary required"
                process = await asyncio.create_subprocess_exec(
                    binary,
                    "app-server",
                    "--listen",
                    "stdio://",
                    cwd=scratch,
                    env={
                        "PATH": os.defpath,
                        "HOME": scratch,
                        "CODEX_HOME": scratch,
                        "LANG": "C.UTF-8",
                    },
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                connection = CodexConnection(process, context, {})
                cleanup.push_async_callback(connection.stop)
                initialized = await connection.call(
                    "initialize",
                    {
                        "clientInfo": {"name": "daimon_harness", "version": "0.1.0"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                assert str(initialized["userAgent"]).startswith(
                    "daimon_harness/" + CODEX_VERSION + " "
                )
                await connection.send({"method": "initialized"})
                await connection.call(
                    "thread/start",
                    {
                        "model": request.model,
                        "modelProvider": "harness",
                        "cwd": scratch,
                        "ephemeral": True,
                        "environments": [],
                        "dynamicTools": [],
                    },
                )
                path = directory / "config.toml"
                assert path.stat().st_uid == 10000
                assert path.stat().st_mode & 0o777 == 0o600
                script = f"""
import os, urllib.request, urllib.error
assert os.geteuid() == {uid}
for path in [{str(path)!r}, '/proc/{os.getpid()}/environ', '/proc/{os.getpid()}/mem',
             '/proc/{process.pid}/environ', '/proc/{process.pid}/mem']:
    try:
        open(path, 'rb').read(1)
        raise AssertionError('tool read private controller state')
    except PermissionError:
        pass
request = urllib.request.Request({(endpoint + "/responses")!r}, data=b'{{}}',
    headers={{'Content-Type':'application/json'}}, method='POST')
try:
    urllib.request.urlopen(request)
    raise AssertionError('tool reached authenticated relay')
except urllib.error.HTTPError as exc:
    assert exc.code == 401
print('tool relay access refused')
"""
                result = await context.bash(
                    shlex.quote(sys.executable) + " -c " + shlex.quote(script)
                )
                assert "exit_code=0" in result, result
                assert "tool relay access refused" in result, result
                # Bad tokens have the same refusal, without producing request inventory.
                async with httpx.AsyncClient() as caller:
                    response = await caller.post(
                        endpoint + "/responses",
                        json={},
                        headers={"Authorization": "Bearer incorrect"},
                    )
                    assert response.status_code == 401
    journal.close()
    print(json.dumps({"service_uid": 10000, "tool_uid": uid, "relay_isolated": True}))


if __name__ == "__main__":
    assert os.geteuid() == 0
    with tempfile.TemporaryDirectory(prefix="codex-relay-probe-") as scratch:
        root = Path(scratch)
        root.chmod(0o711)
        child = os.fork()
        if child == 0:
            try:
                asyncio.run(probe(root / "home"))
            except BaseException:
                import traceback

                traceback.print_exc()
                os._exit(1)
            sys.stdout.flush()
            os._exit(0)
        _, status = os.waitpid(child, 0)
        assert status == 0, status
