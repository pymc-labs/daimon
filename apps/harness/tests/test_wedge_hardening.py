"""Recovery must replace a wedged sandbox without lying about turn outcomes."""

import asyncio
import json
import time
from contextlib import suppress

import httpx
import pytest
from daimon_harness import isolation, kill_uid
from daimon_harness.service import create_app

from .conftest import FakeModelAdapter, turn


@pytest.mark.parametrize("mode", ["single", "multi"])
@pytest.mark.parametrize("trigger", ["cancel", "deadline"])
async def test_never_dying_worker_escalates_without_request_lock(
    tmp_path, monkeypatch, mode, trigger
):
    monkeypatch.setenv("HARNESS_SESSION_MODE", mode)
    monkeypatch.setattr("daimon_harness.service.CLEANUP_TIMEOUT", 0.03)
    monkeypatch.setattr("daimon_harness.service.CANCEL_JOIN_TIMEOUT", 0.03)
    monkeypatch.setattr("daimon_harness.service.CLEANUP_RETRY_DELAY", 0.01)
    release, started = asyncio.Event(), asyncio.Event()
    workers, exits, records = [], [], []
    monkeypatch.setattr(
        "daimon_harness.service.audit_logger.error", lambda raw: records.append(json.loads(raw))
    )

    class StuckModel(FakeModelAdapter):
        async def run(self, context, history):
            history = await super().run(context, history)
            if context.request.session_id != "session-1":
                return history

            async def stuck():
                started.set()
                while not release.is_set():
                    with suppress(asyncio.CancelledError):
                        await release.wait()
                return "released"

            worker = asyncio.create_task(context.tool("stuck", "write", {}, stuck))
            workers.append(worker)
            await worker  # The owner really is stuck before its finally block.
            return history

    app = create_app(home=tmp_path, token="test", adapter=StuckModel())
    supervisor = app.state.supervisor
    monkeypatch.setattr(supervisor, "exit_unhealthy", lambda: exits.append(1))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test", "Accept": "application/json"},
        ) as client,
    ):
        try:
            request = turn()
            request["limits"] = {
                "deadline_s": 0.08 if trigger == "deadline" else 60,
                "max_tool_calls": 10,
            }
            assert (await client.post("/v0/turns", json=request)).status_code == 202
            await asyncio.wait_for(started.wait(), 2)
            if trigger == "cancel":
                response = await client.post(
                    "/v0/turns/turn-1/cancel", params={"session_id": "session-1"}
                )
                assert response.status_code == 503 and response.json()["detail"]["retryable"]
            until = time.monotonic() + 3
            while "session-1" not in supervisor.sealed_sessions and time.monotonic() < until:
                await asyncio.sleep(0.01)
            assert "session-1" in supervisor.sealed_sessions, "cleanup escalation missing"
            assert [record["failures"] for record in records] == [1, 2, 3], (
                "cleanup escalation missing"
            )
            assert not supervisor.submissions.locked()
            assert not await supervisor.io(supervisor.journal.ended, "turn-1")
            for route in ("/readiness", "/v0/healthz"):
                assert (await client.get(route)).status_code == (
                    503 if mode == "single" else 200
                ), "multi mode killed unrelated sessions"
            if mode == "single":
                async with asyncio.timeout(2):
                    while not exits:
                        await asyncio.sleep(0.01)
                assert exits == [1]
            else:
                await supervisor.cleanups["turn-1"]
                replay = await client.get("/v0/sessions/session-1/events")
                assert replay.status_code == 200
                assert any(
                    event["type"] == "error"
                    and event["data"].get("kind") == "cleanup_exhausted"
                    and event["data"]["retryable"] is False
                    for event in replay.json()["events"]
                )
                assert not replay.json()["complete"]  # No terminal with live workers (I5).
                # Session B must still complete AFTER A has exhausted its budget.
                other = turn(turn_id="other", key="other") | {"session_id": "session-2"}
                assert (await client.post("/v0/turns", json=other)).status_code == 202
                await supervisor.tasks["other"]
                assert (
                    await supervisor.io(supervisor.journal.terminal_outcome, "other") == "completed"
                )
                response = await client.post("/v0/turns", json=turn(turn_id="again", key="again"))
                assert response.status_code == 503
                assert response.json()["detail"]["kind"] == "cleanup_exhausted"
                assert response.json()["detail"]["retryable"] is False
                await asyncio.sleep(1.1)
                assert not exits, "multi mode killed unrelated sessions"
        finally:
            release.set()
            await asyncio.gather(*workers, *supervisor.tasks.values(), return_exceptions=True)


@pytest.mark.parametrize("loop", ["agents_sdk", "codex"])
async def test_cancel_terminal_race_stress(tmp_path, monkeypatch, loop):
    monkeypatch.setenv("HARNESS_LOOP", loop)
    app = create_app(home=tmp_path, token="test", adapter=FakeModelAdapter())
    supervisor = app.state.supervisor
    original_io = supervisor.io
    terminal_ready, release_terminal = asyncio.Event(), asyncio.Event()
    gated = False

    async def io(operation, *args):
        if gated and operation == supervisor.durable_append and args[1] == "turn_ended":
            # Outcome has been copied to the terminal payload, but no event is
            # committed yet. Cancellation here used to report a false success.
            terminal_ready.set()
            await release_terminal.wait()
        return await original_io(operation, *args)

    monkeypatch.setattr(supervisor, "io", io)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test", "Accept": "application/json"},
        ) as client,
    ):
        for index in range(300):
            turn_id = f"race-{index}"
            gated = index % 10 == 0
            terminal_ready.clear()
            release_terminal.clear()
            assert (
                await client.post("/v0/turns", json=turn(turn_id=turn_id, key=turn_id))
            ).status_code == 202
            if gated:
                await asyncio.wait_for(terminal_ready.wait(), timeout=2)
            else:
                for _ in range(index % 9):
                    await asyncio.sleep(0)
            cancel = asyncio.create_task(
                client.post(f"/v0/turns/{turn_id}/cancel", params={"session_id": "session-1"})
            )
            if gated:
                async with asyncio.timeout(2):
                    while not supervisor.tasks[turn_id].cancelling():
                        await asyncio.sleep(0)
                release_terminal.set()
            response = await cancel
            await supervisor.tasks[turn_id]
            assert response.status_code == 200
            terminal = await supervisor.io(supervisor.journal.terminal_outcome, turn_id)
            if response.json()["cancelled"]:
                assert terminal == "interrupted", "cancel acknowledged a completed turn"
            if gated:
                assert terminal == "completed" and not response.json()["cancelled"]


@pytest.mark.parametrize("last,expected", [("rw", True), ("rw,hidepid=2", False)])
def test_last_proc_mount_controls_verification(tmp_path, monkeypatch, last, expected):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 0 0:1 / /proc rw - proc proc rw,hidepid=2\n" + f"2 1 0:2 / /proc rw - proc proc {last}\n"
    )
    monkeypatch.setattr(kill_uid.os, "getgroups", lambda: [])
    monkeypatch.setattr(kill_uid.os, "getegid", lambda: 10001)
    assert kill_uid.visible_proc(mountinfo) is expected
    assert not isolation.proc_hides_other_users(mountinfo)


@pytest.mark.parametrize("gid,valid", [("", False), (",gid=10001", False), (",gid=60002", True)])
def test_single_mode_validates_proc_exemption(tmp_path, monkeypatch, gid, valid):
    monkeypatch.delenv("HARNESS_TEST_MODE")
    monkeypatch.setenv("HARNESS_SESSION_MODE", "single")
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"1 0 0:1 / /proc rw - proc proc rw,hidepid=2{gid}\n")
    if valid:
        assert isolation.validate_session_mode(mountinfo=mountinfo) == "single"
    else:
        with pytest.raises(RuntimeError, match="gid=60002"):
            isolation.validate_session_mode(mountinfo=mountinfo)


def test_zombie_leader_with_live_thread_is_alive(tmp_path, monkeypatch):
    from pathlib import Path

    task = tmp_path / "task"
    (task / "9").mkdir(parents=True)
    (task / "10").mkdir()
    (task / "9" / "stat").write_text("9 (leader) Z 1")
    (task / "10" / "stat").write_text("10 (worker) S 1")
    real_read, real_iter = Path.read_text, Path.iterdir
    monkeypatch.setattr(
        Path, "read_text", lambda p: "9 (leader) Z 1" if str(p) == "/proc/9/stat" else real_read(p)
    )
    monkeypatch.setattr(
        Path, "iterdir", lambda p: real_iter(task) if str(p) == "/proc/9/task" else real_iter(p)
    )
    assert not kill_uid.terminated(9)
    (task / "10" / "stat").write_text("10 (worker) Z 1")
    assert kill_uid.terminated(9)


async def test_file_cleanup_bounds_child_wait(tmp_path, monkeypatch):
    from daimon_harness.runtime import TurnContext, discard_event
    from mux.drivers.selfhosted.wire import TurnRequest

    monkeypatch.setattr("daimon_harness.runtime.CLEANUP_WAIT_TIMEOUT", 0.02)
    monkeypatch.setattr("daimon_harness.runtime.os.killpg", lambda *args: None)

    class Child:
        pid = 123
        returncode = 0

        async def communicate(self, payload):
            return b'{"output":"value"}', b""

        async def wait(self):
            await asyncio.Event().wait()

    async def spawn(*args, **kwargs):
        return Child()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    context = TurnContext(TurnRequest.model_validate(turn()), tmp_path, discard_event)
    async with asyncio.timeout(0.5):
        with pytest.raises(TimeoutError):
            await context.async_file_operation("read", "file")


def test_startup_sweep_kills_each_orphan_uid_and_checks_exit(tmp_path, monkeypatch):
    from pathlib import Path

    from daimon_harness import startup_sweep

    proc = tmp_path / "proc"
    for pid, uid in [(1, 0), (2, 10000), (3, 10001), (4, 10001), (5, 10002)]:
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "status").write_text(f"Uid:\t{uid} {uid} {uid} {uid}\n")
    real_iter = Path.iterdir
    monkeypatch.setattr(
        Path, "iterdir", lambda p: real_iter(proc) if str(p) == "/proc" else real_iter(p)
    )
    calls = []
    monkeypatch.setattr(
        startup_sweep.subprocess, "run", lambda command, **kw: calls.append((command, kw))
    )
    startup_sweep.sweep()
    assert [call[0][-1] for call in calls] == ["10001", "10002"]
    assert all(call[1]["check"] and call[1]["timeout"] == 2 for call in calls)

    def failed(*args, **kwargs):
        raise startup_sweep.subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(startup_sweep.subprocess, "run", failed)
    with pytest.raises(startup_sweep.subprocess.CalledProcessError):
        startup_sweep.sweep()


async def test_cancel_does_not_wait_for_slow_initial_cleanup(tmp_path, monkeypatch):
    from daimon_harness.runtime import TurnContext

    monkeypatch.setattr("daimon_harness.service.CANCEL_JOIN_TIMEOUT", 0.02)
    entered, release = asyncio.Event(), asyncio.Event()
    original = TurnContext.stop_workers

    async def slow(context):
        entered.set()
        await release.wait()
        await original(context)

    monkeypatch.setattr(TurnContext, "stop_workers", slow)
    app = create_app(home=tmp_path, token="test", adapter=FakeModelAdapter())
    supervisor = app.state.supervisor
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test", "Accept": "application/json"},
        ) as client,
    ):
        try:
            await client.post("/v0/turns", json=turn())
            await asyncio.wait_for(entered.wait(), timeout=2)
            before = time.monotonic()
            response = await client.post(
                "/v0/turns/turn-1/cancel", params={"session_id": "session-1"}
            )
            assert response.status_code == 503
            assert response.json()["detail"]["kind"] == "workers_active"
            assert response.json()["detail"]["retryable"]
            assert time.monotonic() - before < 0.75
            assert not supervisor.submissions.locked()
            assert not await supervisor.io(supervisor.journal.ended, "turn-1")
        finally:
            release.set()
            await supervisor.tasks["turn-1"]
        assert await supervisor.io(supervisor.journal.terminal_outcome, "turn-1") == "interrupted"
