"""Termination verification must fail closed on processes surviving SIGKILL."""

import asyncio
import signal
import subprocess
from pathlib import Path

import pytest
from daimon_harness import kill_uid
from daimon_harness.runtime import TurnContext

from .conftest import turn
from .test_checkpoints import MemoryBlobs, harness


@pytest.mark.parametrize("survivors", [True, False])
def test_kill_uid_deadline_requires_no_remaining_processes(monkeypatch, survivors):
    probes = []

    def kill(pid, sig):
        probes.append((pid, sig))
        if sig == 0 and (pid != 9 or not survivors):
            raise ProcessLookupError

    monkeypatch.setattr(kill_uid, "visible_proc", lambda: True)
    monkeypatch.setattr(kill_uid, "present_pids", lambda: [1, 8, 9])
    monkeypatch.setattr(kill_uid.os, "kill", kill)
    monkeypatch.setattr(kill_uid.os, "getpid", lambda: 8)
    monkeypatch.setattr(kill_uid, "terminated", lambda pid: False)
    monkeypatch.setattr(kill_uid.Path, "read_text", lambda self: "9")
    assert kill_uid.kill_uid(timeout=0) == int(survivors)
    assert probes[0] == (-1, signal.SIGKILL)
    assert (9, 0) in probes


async def test_kill_uid_failure_keeps_i5_seal(tmp_path, monkeypatch):
    original = TurnContext.stop_processes

    def failed_kill(self):
        raise subprocess.CalledProcessError(1, ["kill_uid.py"])

    async with harness(tmp_path, MemoryBlobs()) as (supervisor, client):
        with monkeypatch.context() as patch:
            patch.setattr(TurnContext, "stop_processes", failed_kill)
            assert (await client.post("/v0/turns", json=turn())).status_code == 202
            result = await asyncio.gather(supervisor.tasks["turn-1"], return_exceptions=True)
            assert isinstance(result[0], subprocess.CalledProcessError)
            for _ in range(2):
                response = await client.get("/v0/sessions/session-1/events")
                assert response.status_code == 503
                assert response.json()["detail"]["kind"] == "workers_active"
                response = await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
                assert response.status_code == 503
        assert TurnContext.stop_processes is original
        response = await client.get("/v0/sessions/session-1/events")
        assert response.status_code == 200 and response.json()["complete"]
        assert (
            await client.post("/v0/turns", json=turn(turn_id="next", key="next"))
        ).status_code == 202
        await supervisor.tasks["next"]


@pytest.mark.parametrize("state,dead", [("Z", True), ("X", True), ("D", False), ("R", False)])
def test_termination_check_distinguishes_zombies_from_live_syscalls(monkeypatch, state, dead):
    monkeypatch.setattr(kill_uid.Path, "read_text", lambda self: f"9 (a ) tricky name) {state} 1")
    assert kill_uid.terminated(9) is dead


@pytest.mark.parametrize("error", [PermissionError, FileNotFoundError])
def test_hidden_process_is_not_assumed_dead(monkeypatch, error):
    def denied(self):
        raise error

    monkeypatch.setattr(kill_uid.Path, "read_text", denied)
    monkeypatch.setattr(kill_uid.os, "kill", lambda pid, sig: None)
    assert not kill_uid.terminated(9)


@pytest.mark.parametrize("counter", [4_000_000, 9])
@pytest.mark.parametrize("survivor", [False, True])
def test_sparse_four_million_pid_namespace_and_wrap(monkeypatch, counter, survivor):
    probes = []

    def kill(pid, sig):
        probes.append(pid)
        if sig == 0 and (pid != 3_999_999 or not survivor):
            raise ProcessLookupError

    monkeypatch.setattr(kill_uid, "visible_proc", lambda: True)
    monkeypatch.setattr(
        kill_uid.Path,
        "iterdir",
        lambda self: iter(map(Path, ["/proc/1", "/proc/8", "/proc/3999999", "/proc/self"])),
    )
    monkeypatch.setattr(kill_uid.Path, "read_text", lambda self: str(counter))
    monkeypatch.setattr(kill_uid.os, "getpid", lambda: 8)
    monkeypatch.setattr(kill_uid.os, "kill", kill)
    monkeypatch.setattr(kill_uid, "terminated", lambda pid: False)
    assert kill_uid.kill_uid(timeout=0) == int(survivor), "sparse PID cleanup failed"
    assert len(probes) == 2, "cleanup scanned the PID allocation range"
    assert 3_999_999 in probes, "PID wrap hid a surviving process"


@pytest.mark.parametrize(
    "options,groups,visible",
    [
        ("", [], True),
        (",hidepid=0", [], True),
        (",hidepid=1", [], False),
        (",hidepid=2", [], False),
        (",hidepid=invisible,gid=60002", [], False),
        (",hidepid=2,gid=60002", [60002], True),
        (",hidepid=2,gid=10002", [60002], False),
        (",hidepid=4,gid=60002", [60002], False),
        (",hidepid=ptraceable,gid=60002", [60002], False),
    ],
)
def test_proc_enumeration_requires_verified_visibility(
    tmp_path, monkeypatch, options, groups, visible
):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"25 24 0:22 / /proc rw - proc proc rw{options}\n")
    monkeypatch.setattr(kill_uid.os, "getgroups", lambda: groups)
    monkeypatch.setattr(kill_uid.os, "getegid", lambda: 10001)
    assert kill_uid.visible_proc(mountinfo) == visible


def test_hidden_proc_mount_fails_closed_before_enumerating(monkeypatch):
    monkeypatch.setattr(kill_uid, "visible_proc", lambda: False)
    monkeypatch.setattr(kill_uid, "present_pids", lambda: pytest.fail("hidden enumeration"))
    assert kill_uid.kill_uid() == 1
