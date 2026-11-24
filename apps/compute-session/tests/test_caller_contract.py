"""The real control-plane caller against the real server, with no network between them.

`FlyComputeProvider.call` sends the requests and reads the result, and the background
command scripts of `daimon.core.compute_tasks` run through `run_bash`, so a change on
either side that breaks the contract of `docs/compute.md` fails here.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import signal
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from compute_session import tools
from daimon.core import compute_tasks as scripts
from daimon.core.compute_fly import FlyComputeProvider
from daimon.core.compute_models import ComputeSession
from daimon.core.compute_provider import ComputeProviderError, app_name
from daimon.core.config import ComputeSettings
from daimon.testing.compute import FakeComputeProvider
from pydantic import JsonValue, SecretStr
from starlette.types import ASGIApp

from .conftest import TOKEN

pytestmark = pytest.mark.skipif(
    not all(shutil.which(name) for name in ("bash", "sh", "setsid", "timeout", "base64", "tail")),
    reason="needs the shell tools the session image carries",
)

DEPLOYMENT = "deployment-a"


@pytest.fixture
def provider(app: ASGIApp) -> FlyComputeProvider:
    settings = ComputeSettings(
        deployment_id=DEPLOYMENT,
        org_slug="org",
        api_token=SecretStr("fly-test-token"),
        session_auth_key=SecretStr("hmac-test-key"),
    )
    return FlyComputeProvider(settings, transport=httpx.ASGITransport(app=app))


@pytest.fixture
def session() -> ComputeSession:
    session_id = uuid.uuid4()
    return ComputeSession(
        id=session_id,
        tenant_id=uuid.uuid4(),
        owner_account_id=uuid.uuid4(),
        owner_external_user_id="user",
        deployment_id=DEPLOYMENT,
        platform="discord",
        parent_channel_id="parent",
        thread_id="thread",
        agent_id="agent",
        agent_name="a",
        request_key="request",
        app_name=app_name(DEPLOYMENT, session_id),
        profile="work",
        image="registry.example/compute-session@sha256:abc",
        region="iad",
        cpu_kind="shared",
        cpus=2,
        memory_mb=4096,
        ttl_hours=24,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        created_at=datetime.now(UTC),
    )


async def test_inline_bash_answers_in_the_shape_the_fake_promises(
    provider: FlyComputeProvider, session: ComputeSession
) -> None:
    result = await provider.call(
        session, TOKEN, "run_bash", {"command": "printf done", "timeout_s": 40}
    )
    promised = FakeComputeProvider().result
    assert isinstance(result, dict) and isinstance(promised, dict)
    assert result == promised | {"truncated": False}


async def test_file_tools_take_the_arguments_the_mcp_adapter_sends(
    provider: FlyComputeProvider, session: ComputeSession, tmp_path: Path
) -> None:
    path = str(tmp_path / "out" / "result.txt")
    written = await provider.call(session, TOKEN, "write_file", {"path": path, "content": "abc"})
    assert written == {"path": path, "bytes": 3}
    read = await provider.call(
        session, TOKEN, "read_file", {"path": path, "offset": 1, "limit": 65536}
    )
    assert read == {
        "path": path,
        "size": 3,
        "offset": 1,
        "bytes": 2,
        "encoding": "utf-8",
        "content": "bc",
        "eof": True,
    }
    listed = await provider.call(
        session, TOKEN, "list_files", {"path": str(tmp_path / "out"), "pattern": "*"}
    )
    assert listed == {
        "path": str(tmp_path / "out"),
        "count": 1,
        "entries": [{"name": "result.txt", "is_dir": False, "size": 3}],
        "truncated": False,
    }


async def test_the_caller_reports_a_wrong_token_and_a_refused_call(
    provider: FlyComputeProvider, session: ComputeSession, tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    with pytest.raises(ComputeProviderError, match="did not answer"):
        await provider.call(session, "another-token", "run_bash", {"command": f"touch {marker}"})
    with pytest.raises(ComputeProviderError, match="refused"):
        await provider.call(
            session, TOKEN, "run_bash", {"command": f"touch {marker}", "timeout_s": 0}
        )
    with pytest.raises(ComputeProviderError, match="refused"):
        await provider.call(session, TOKEN, "upload_file", {"path": str(marker)})
    assert not marker.exists()


async def test_the_largest_read_the_adapter_allows_fits_the_callers_body_limit(
    provider: FlyComputeProvider, session: ComputeSession, tmp_path: Path
) -> None:
    """4 MiB of quotes is 8 MiB as JSON, twice: the caller would drop it at 8 MiB."""
    path = tmp_path / "quotes.txt"
    path.write_bytes(b'"' * tools.MAX_READ_BYTES)
    result = await provider.call(
        session, TOKEN, "read_file", {"path": str(path), "offset": 0, "limit": tools.MAX_READ_BYTES}
    )
    assert isinstance(result, dict) and isinstance(result["bytes"], int)
    assert 0 < result["bytes"] < tools.MAX_READ_BYTES
    assert (result["size"], result["eof"]) == (tools.MAX_READ_BYTES, False)


async def test_a_text_file_read_in_chunks_has_the_same_hash(
    provider: FlyComputeProvider, session: ComputeSession, tmp_path: Path
) -> None:
    """The adapter's read tool works in byte chunks; its default chunk is 65536 bytes."""
    path = tmp_path / "report.txt"
    path.write_text("naïve €uro 😀 data, 日本語\n" * 7000, encoding="utf-8")
    assert path.stat().st_size > 3 * 65536
    text, offset = "", 0
    while True:
        chunk = await provider.call(
            session, TOKEN, "read_file", {"path": str(path), "offset": offset, "limit": 65536}
        )
        assert isinstance(chunk, dict) and chunk["encoding"] == "utf-8"
        assert isinstance(chunk["content"], str) and isinstance(chunk["bytes"], int)
        assert 65536 - 4 < chunk["bytes"] <= 65536 or chunk["eof"]
        text, offset = text + chunk["content"], offset + chunk["bytes"]
        if chunk["eof"]:
            break
    assert "\ufffd" not in text
    assert hashlib.sha256(text.encode()).digest() == hashlib.sha256(path.read_bytes()).digest()


async def _probe(
    provider: FlyComputeProvider, session: ComputeSession, task_id: uuid.UUID, task_dir: str
) -> scripts.Probe:
    # The arguments compute_sessions sends when it polls a task.
    arguments: dict[str, JsonValue] = {
        "command": scripts.probe_script(task_id, task_dir=task_dir),
        "timeout_s": 20,
    }
    return scripts.parse_probe(await provider.call(session, TOKEN, "run_bash", arguments))


async def test_a_background_command_starts_once_and_reports_its_end(
    provider: FlyComputeProvider, session: ComputeSession, tmp_path: Path
) -> None:
    task_dir, task_id = str(tmp_path / "tasks"), uuid.uuid4()
    started = time.monotonic()
    start = scripts.start_script(
        task_id, "echo hi; echo oops >&2; exit 3", timeout_s=60, task_dir=task_dir
    )
    first = await provider.call(session, TOKEN, "run_bash", {"command": start, "timeout_s": 30})
    scripts.parse_start(first)
    assert isinstance(first, dict) and first["stdout"] == "started\n"

    marker = tmp_path / "second-run"
    again = scripts.start_script(task_id, f"touch {marker}", timeout_s=60, task_dir=task_dir)
    second = await provider.call(session, TOKEN, "run_bash", {"command": again, "timeout_s": 30})
    scripts.parse_start(second)
    assert isinstance(second, dict) and second["stdout"] == "exists\n"

    probe = await _probe(provider, session, task_id, task_dir)
    while probe.status != "exit" and time.monotonic() - started < 10:
        await asyncio.sleep(0.02)
        probe = await _probe(provider, session, task_id, task_dir)
    assert (probe.status, probe.exit_code) == ("exit", 3)
    assert (probe.tail, probe.output_bytes) == ("hi\noops\n", 8)
    assert not marker.exists(), "a repeated start must not run the command again"

    # fly_task_result points the owner at the output file for more than the tail.
    output = await provider.call(
        session,
        TOKEN,
        "read_file",
        {"path": scripts.output_path(task_id, task_dir=task_dir), "offset": 0, "limit": 65536},
    )
    assert isinstance(output, dict) and output["content"] == "hi\noops\n"


async def test_a_detached_command_outlives_its_start_call(
    provider: FlyComputeProvider, session: ComputeSession, tmp_path: Path
) -> None:
    task_dir, task_id = str(tmp_path / "tasks"), uuid.uuid4()
    started = time.monotonic()
    start = scripts.start_script(task_id, "echo up; sleep 30", timeout_s=60, task_dir=task_dir)
    scripts.parse_start(
        await provider.call(session, TOKEN, "run_bash", {"command": start, "timeout_s": 30})
    )
    assert time.monotonic() - started < 5, "a detached command must not hold the call open"
    group = Path(task_dir, task_id.hex + ".group")
    try:
        probe = await _probe(provider, session, task_id, task_dir)
        while probe.tail != "up\n" and time.monotonic() - started < 10:
            await asyncio.sleep(0.02)
            probe = await _probe(provider, session, task_id, task_dir)
        assert (probe.status, probe.exit_code, probe.tail) == ("running", None, "up\n")
        assert (await _probe(provider, session, uuid.uuid4(), task_dir)).status == "missing"
    finally:
        # What the kill script does, without its two-second pause between signals.
        os.killpg(int(group.read_text()), signal.SIGKILL)
