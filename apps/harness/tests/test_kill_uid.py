"""Termination verification must fail closed on processes surviving SIGKILL."""

import asyncio
import signal
import subprocess

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
