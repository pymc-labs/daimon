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
    monkeypatch.setattr("daimon_harness.service.CLEANUP_BACKOFF_CAP", 0.03)
    release, started, deadline = asyncio.Event(), asyncio.Event(), asyncio.Event()
    workers, exits, records = [], [], []
    exhausted = asyncio.Event()
    monkeypatch.setattr("daimon_harness.service.audit_logger.critical", lambda raw: exhausted.set())
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
            await worker
            return history

    app = create_app(home=tmp_path, token="test", adapter=StuckModel())
    supervisor = app.state.supervisor

    async def clock(request):
        if request.session_id == "session-1":
            await deadline.wait()
        else:
            await asyncio.Event().wait()

    monkeypatch.setattr(supervisor, "watchdog_delay", clock)
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
            request = turn() | {"limits": {"deadline_s": 60, "max_tool_calls": 10}}
            assert (await client.post("/v0/turns", json=request)).status_code == 202
            await asyncio.wait_for(started.wait(), 30)
            if trigger == "cancel":
                response = await client.post(
                    "/v0/turns/turn-1/cancel", params={"session_id": "session-1"}
                )
                assert response.status_code == 503 and response.json()["detail"]["retryable"]
            else:
                deadline.set()  # Deterministic expiry AFTER the adapter is awaiting its worker.
            try:
                await asyncio.wait_for(exhausted.wait(), 10)
            except TimeoutError:
                raise AssertionError("cleanup escalation missing") from None
            assert supervisor.cleanup_failures.get("turn-1", 0) >= 3
            assert [record["failures"] for record in records[:3]] == [1, 2, 3]
            assert not supervisor.submissions.locked()
            assert not await supervisor.io(supervisor.journal.ended, "turn-1")
            for route in ("/readiness", "/v0/healthz"):
                assert (await client.get(route)).status_code == (
                    503 if mode == "single" else 200
                ), "multi mode killed unrelated sessions"
            if mode == "single":
                response = await client.post(
                    "/v0/turns/turn-1/cancel", params={"session_id": "session-1"}
                )
                assert response.json()["detail"]["retryable"] is True
                assert "new conversation" not in response.text
                async with asyncio.timeout(10):
                    while not exits:
                        await asyncio.sleep(0.01)
            else:
                other = turn(turn_id="other", key="other") | {"session_id": "session-2"}
                assert (await client.post("/v0/turns", json=other)).status_code == 202
                await supervisor.tasks["other"]
                assert (
                    await supervisor.io(supervisor.journal.terminal_outcome, "other") == "completed"
                )
                before = len(records)
                await asyncio.sleep(0.1)
                assert len(records) > before, "sealed cleanup stopped retrying"
                response = await client.post("/v0/turns", json=turn(turn_id="again", key="again"))
                assert response.json()["detail"]["retryable"] is False
                assert not exits
        finally:
            release.set()
            await asyncio.gather(*workers, *supervisor.tasks.values(), return_exceptions=True)
            await supervisor.cleanups["turn-1"]
        if mode == "multi":
            replay = await client.get("/v0/sessions/session-1/events")
            assert replay.json()["complete"] is True
            terminal = replay.json()["events"][-1]
            assert terminal["type"] == "turn_ended"
            assert terminal["data"]["error"]["kind"] == "cleanup_exhausted"
            assert terminal["data"]["error"]["retryable"] is False


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
    assert all(call[1]["check"] and call[1]["timeout"] == 4 for call in calls)

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


async def test_cancel_timeout_holds_uid_until_old_verifier_finishes(tmp_path, monkeypatch):
    import threading

    from daimon_harness.runtime import TurnContext

    monkeypatch.setattr("daimon_harness.service.CANCEL_JOIN_TIMEOUT", 0.02)
    monkeypatch.setattr("daimon_harness.runtime.VERIFY_WAIT_TIMEOUT", 0.02)
    entered, release_thread = threading.Event(), threading.Event()
    owner_released, started = asyncio.Event(), asyncio.Event()
    original_stop = TurnContext._stop_processes
    calls = []

    def held(context):
        calls.append(context.request.turn_id)
        if len(calls) == 1:
            entered.set()
            assert release_thread.wait(5)
        original_stop(context)

    monkeypatch.setattr(TurnContext, "_stop_processes", held)

    class Model(FakeModelAdapter):
        async def run(self, context, history):
            history = await super().run(context, history)
            if context.request.turn_id == "turn-1":
                started.set()
                while not owner_released.is_set():
                    with suppress(asyncio.CancelledError):
                        await owner_released.wait()
            else:
                value = await context.tool(
                    "survive", "bash", {}, lambda: context.bash("sleep 0.1; echo survived")
                )
                assert "exit_code=0" in value and "survived" in value, (
                    "old cleanup killed the next turn"
                )
            return history

    app = create_app(home=tmp_path, token="test", adapter=Model())
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
            await started.wait()
            context = supervisor.contexts["turn-1"]
            old = asyncio.create_task(context.verify_processes())
            assert await asyncio.to_thread(entered.wait, 2)
            with pytest.raises(TimeoutError):
                await old
            response = await client.post(
                "/v0/turns/turn-1/cancel", params={"session_id": "session-1"}
            )
            assert response.status_code == 503
            owner_released.set()  # Owner enters finalization while the thread is still live.
            await asyncio.sleep(0)
            next_turn = turn(turn_id="next", key="next")
            assert (await client.post("/v0/turns", json=next_turn)).status_code == 503, (
                "UID released with old cleanup in flight"
            )
            replay = await client.get("/v0/sessions/session-1/events")
            assert replay.status_code == 200 and not replay.json()["complete"]
            release_thread.set()
            await supervisor.tasks["turn-1"]
            await supervisor.cleanups["turn-1"]
            assert (await client.post("/v0/turns", json=next_turn)).status_code == 202
            await supervisor.tasks["next"]
        finally:
            owner_released.set()
            release_thread.set()


async def test_healthy_two_twelve_second_commits_do_not_escalate(tmp_path, monkeypatch):
    from daimon_harness import service

    assert service.CLEANUP_TIMEOUT == 10
    assert service.owner_tail_bound(1) >= 17 + 10 + 2 * 15 + 5
    started = asyncio.Event()
    commits, exits = [], []

    class Model(FakeModelAdapter):
        async def run(self, context, history):
            await context.emit(
                "model_request_started", {"request_id": "pending", "model": context.request.model}
            )
            started.set()
            await asyncio.Event().wait()

    app = create_app(home=tmp_path, token="test", adapter=Model())
    supervisor = app.state.supervisor
    original_io = supervisor.io

    async def slow_commit(operation, *args):
        if operation == supervisor.durable_append and args[1] in {"error", "turn_ended"}:
            commits.append(args[1])
            await asyncio.sleep(12)  # Real constants, two legal (<15 s) WAL commits.
        return await original_io(operation, *args)

    monkeypatch.setattr(supervisor, "io", slow_commit)
    monkeypatch.setattr(supervisor, "exit_unhealthy", lambda: exits.append(1))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test", "Accept": "application/json"},
        ) as client,
    ):
        body = turn() | {"limits": {"max_tool_calls": 10, "deadline_s": 60}}
        await client.post("/v0/turns", json=body)
        await started.wait()
        response = await client.post("/v0/turns/turn-1/cancel", params={"session_id": "session-1"})
        assert response.status_code == 503
        await supervisor.tasks["turn-1"]
        await supervisor.cleanups["turn-1"]
        assert commits == ["error", "turn_ended"]
        assert not supervisor.cleanup_failures and not exits and not supervisor.unhealthy, (
            "healthy durable tail escalated"
        )
        assert await supervisor.io(supervisor.journal.terminal_outcome, "turn-1") == "interrupted"


async def test_watchdog_starts_after_write_ahead_and_stops_before_finalization(
    tmp_path, monkeypatch
):
    from daimon_harness.runtime import TurnContext

    wal_entered, wal_release = asyncio.Event(), asyncio.Event()
    finalizing, finish = asyncio.Event(), asyncio.Event()
    watchdog_started, expiry = asyncio.Event(), asyncio.Event()
    original = TurnContext.stop_workers

    async def held_cleanup(context):
        finalizing.set()
        await finish.wait()
        await original(context)

    monkeypatch.setattr(TurnContext, "stop_workers", held_cleanup)
    app = create_app(home=tmp_path, token="test", adapter=FakeModelAdapter())
    supervisor = app.state.supervisor

    async def wal(request):
        wal_entered.set()
        await wal_release.wait()

    async def clock(request):
        watchdog_started.set()
        await expiry.wait()

    monkeypatch.setattr(supervisor, "write_ahead", wal)
    monkeypatch.setattr(supervisor, "watchdog_delay", clock)
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
            await wal_entered.wait()
            assert not watchdog_started.is_set(), "watchdog started before write-ahead"
            wal_release.set()
            await finalizing.wait()
            expiry.set()
            await asyncio.sleep(0)
            finish.set()
            await supervisor.tasks["turn-1"]
            await supervisor.cleanups["turn-1"]
            assert (
                await supervisor.io(supervisor.journal.terminal_outcome, "turn-1") == "completed"
            ), "watchdog relabeled completion"
        finally:
            wal_release.set()
            finish.set()


async def test_single_exit_rechecks_verified_cleanup(tmp_path, monkeypatch):
    from daimon_harness.runtime import TurnContext, discard_event
    from mux.drivers.selfhosted.wire import TurnRequest

    app = create_app(home=tmp_path, token="test", adapter=FakeModelAdapter())
    supervisor = app.state.supervisor
    exits = []
    monkeypatch.setattr("daimon_harness.service.os._exit", lambda code: exits.append(code))
    supervisor.unhealthy = True
    supervisor.cleanup_failures["turn"] = 3
    supervisor.contexts["turn"] = TurnContext(
        TurnRequest.model_validate(turn()), tmp_path / "tool", discard_event
    )
    supervisor.exit_unhealthy()
    assert exits == [1]
    exits.clear()
    supervisor.cleanup_verified.add("turn")
    supervisor.exit_unhealthy()
    assert not exits, "replacement ignored successful cleanup"
    await supervisor.close()
