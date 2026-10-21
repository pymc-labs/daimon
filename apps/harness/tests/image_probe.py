"""Standalone root-image probe, run in CI without Azure credentials."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from daimon_harness.isolation import validate_session_mode
from daimon_harness.journal import ConflictError, Journal
from daimon_harness.runtime import TurnContext, discard_event
from daimon_harness.service import create_app
from mux.drivers.selfhosted.wire import InlineSkill, TurnRequest


async def checkpoint_probe(home: Path) -> None:
    """Exercise publication with the real service caps and tool-owned workspace."""
    from daimon_harness.checkpoints import Checkpoints

    class Blobs:
        def __init__(self):
            self.data = {}

        def list(self, prefix):
            return [name for name in self.data if name.startswith(prefix)]

        def upload(self, name, source):
            self.data[name] = source.read()

        def download(self, name, target):
            target.write(self.data[name])

        def delete(self, name):
            del self.data[name]

        def close(self):
            pass

    assert os.geteuid() == 10000
    status = Path("/proc/self/status").read_text()
    assert "CapEff:\t00000000000000e1" in status, status
    blobs = Blobs()
    checkpoints = Checkpoints(blobs)
    old = Journal(home / "checkpoint-old")
    request = TurnRequest(
        session_id="probe",
        turn_id="probe",
        idempotency_key="probe",
        model="test",
        instructions="",
        input=[],
        mcp_servers=[],
        skills=[],
        limits={"max_tool_calls": 1, "deadline_s": 10},
    )
    old.bind_session("probe")
    old.claim(request)
    sandbox, uid = old.sandbox("probe")
    context = TurnContext(
        request, sandbox, discard_event, tool_uid=uid, state_dir=old.session("probe")
    )
    await context.bash(
        "printf persisted > result; printf '#!/bin/sh\\necho executable\\n' > run; chmod 7700 run; python -m venv .venv; mkfifo pipe; touch unreadable; chmod 000 unreadable"
    )
    assert "home =" in context.read(".venv/pyvenv.cfg")
    old.append(request, "turn_ended", {"outcome": "completed"})
    report = checkpoints.save(old, "probe")
    assert report["degraded"] is True
    assert report["skipped_count"] >= 3, report
    # A normal venv and special entries must not prevent later snapshots.
    old.append(request, "status_running", {})
    checkpoints.save(old, "probe")
    old.close()
    fresh = Journal(home / "checkpoint-new")
    restored = checkpoints.restore(fresh, "probe")
    sandbox, uid = restored.sandbox("probe")
    context = TurnContext(
        request, sandbox, discard_event, tool_uid=uid, state_dir=restored.session("probe")
    )
    assert await context.bash("cat result; ./run") == "exit_code=0\npersistedexecutable\n"
    modes = await context.bash("stat -c '%a' run result; test ! -e pipe && echo skipped")
    assert modes == "exit_code=0\n700\n600\nskipped\n", modes
    assert restored.events("probe")[-1].type == "status_running"
    assert (sandbox / "work").stat().st_uid == uid
    restored.close()


async def main() -> None:
    assert os.geteuid() == 0
    home = Path(os.environ.get("HARNESS_PROBE_HOME", "/home/harness"))
    secret = "synthetic-runtime-secret"
    os.environ["HARNESS_TOKEN"] = secret
    os.environ["AZURE_OPENAI_API_KEY"] = secret
    projected = Path("/var/run/secrets/azure/tokens/azure-identity-token")
    projected.parent.mkdir(parents=True, exist_ok=True)
    projected.write_text("synthetic-projected-identity-token")
    projected.chmod(0o644)
    try:
        try:
            create_app(home=home)
            raise AssertionError("accepted a mounted workload identity token")
        except RuntimeError as exc:
            assert "workload identity" in str(exc), str(exc)
        assert os.geteuid() == 0, "credential refusal must precede UID drop and tool startup"
    finally:
        projected.unlink()
    operator_file = Path("/run/secrets/operator-credential")
    operator_file.parent.mkdir(parents=True, exist_ok=True)
    operator_file.write_text("synthetic-unreferenced-secret")
    operator_file.chmod(0o644)
    try:
        try:
            create_app(home=home)
            raise AssertionError("accepted a tool-readable unreferenced secret file")
        except RuntimeError as exc:
            assert "not be readable" in str(exc), str(exc)
        assert os.geteuid() == 0
    finally:
        operator_file.unlink()
    if os.environ.get("HARNESS_SESSION_MODE") == "multi":
        for scratch in (Path("/var/tmp"), Path("/dev/shm")):
            scratch.chmod(0o1777)
            try:
                validate_session_mode()
                raise AssertionError(f"accepted writable {scratch}")
            except RuntimeError as exc:
                assert str(scratch) in str(exc), str(exc)
            finally:
                scratch.chmod(0o755)
    app = create_app(home=home)
    mode = app.state.supervisor.session_mode
    assert os.geteuid() == 10000
    journal: Journal = app.state.supervisor.journal
    contexts: list[TurnContext] = []
    for name in ("first", "second") if mode == "multi" else ("first",):
        sandbox, uid = journal.sandbox(name)
        request = TurnRequest(
            session_id=name,
            turn_id=name,
            idempotency_key=name,
            model="gpt-6-luna",
            instructions="",
            input=[],
            mcp_servers=[],
            skills=[InlineSkill(name="sample", files={"SKILL.md": "readable skill"})],
            limits={"max_tool_calls": 1, "deadline_s": 5},
        )
        context = TurnContext(
            request, sandbox, discard_event, tool_uid=uid, state_dir=journal.session(name)
        )
        await context.skills()
        context.write("file.txt", name)
        contexts.append(context)
    first = contexts[0]
    assert await first.async_file_operation("write", "async.txt", content="one two") == "written"
    assert await first.async_file_operation("edit", "async.txt", old="two", new="three") == "edited"
    assert await first.async_file_operation("read", "async.txt") == "one three"
    assert not first.process_groups
    if mode == "single":
        # Exercise the production supervisor, including a binding surviving restart.
        journal.bind_session("first")
        other = first.request.model_copy(update={"session_id": "second"})
        try:
            await app.state.supervisor.submit(other)
            raise AssertionError("single mode accepted a second session")
        except ConflictError as exc:
            assert str(exc) == "session_bound"
        foreign_check = ""
    else:
        second = contexts[1]
        foreign_check = f"cat {second.work}/file.txt 2>/dev/null; "
    output = await first.bash(
        f"env; cat /proc/{os.getpid()}/environ 2>/dev/null; "
        f"cat /proc/{os.getpid()}/cmdline 2>/dev/null; "
        f"{foreign_check}"
        f"test -r {journal.root}/turns.sqlite3 && echo journal-readable; "
        f"test -w {journal.root} && echo journal-writable; "
        f"test -w {first.home}/skills && echo skills-writable; "
        f"cat {first.home}/skills/sample/SKILL.md; id -u; "
        "awk '/CapEff/{print $2}' /proc/self/status"
    )
    assert secret not in output, output
    assert "second" not in output, output
    assert "journal-readable" not in output, output
    assert "journal-writable" not in output, output
    assert "skills-writable" not in output, output
    assert "readable skill" in output, output
    assert "10001" in output, output
    assert "0000000000000000" in output, output
    assert await first.bash("cat file.txt") == "exit_code=0\nfirst"
    temporary = await first.bash('printf private-tmp > "$TMPDIR/report"; printf "%s" "$TMPDIR"')
    assert str(first.home / "tmp") in temporary, temporary
    if mode == "multi":
        # A deliberately detached process keeps a private value in argv while B probes it.
        sleeper = (
            "import os,time; from pathlib import Path; "
            'Path(os.environ["TMPDIR"], "ready").write_text(str(os.getpid())); time.sleep(30)'
        )
        process = subprocess.Popen(
            [sys.executable, "-c", sleeper, "private-command-marker"],
            user=first.tool_uid,
            group=first.tool_uid,
            extra_groups=[],
            env={"TMPDIR": str(first.home / "tmp")},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pid = process.pid
        try:
            own = subprocess.run(
                ["/bin/bash", "-c", f"cat /proc/{pid}/cmdline; cat {first.home}/tmp/report"],
                user=first.tool_uid,
                group=first.tool_uid,
                extra_groups=[],
                env={},
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            ).stdout
            assert "private-command-marker" in own, own
            assert "private-tmp" in own, own
            assert process.poll() is None
            output = await second.bash(
                f"cat {first.home}/tmp/report /proc/{pid}/cmdline 2>/dev/null; "
                "test -w /tmp && echo global-tmp-writable; echo isolated"
            )
            assert "private-tmp" not in output, output
            assert "private-command-marker" not in output, output
            assert "global-tmp-writable" not in output, output
            assert "isolated" in output, output
        finally:
            process.kill()
            process.wait(timeout=2)
    # A detached tool process races parent-directory replacement while the service
    # asks the file helper to read/write/edit. The private target is service-owned.
    protected = journal.root / "race-target"
    protected.mkdir(exist_ok=True)
    private = protected / "file"
    private.write_text("inside-private-journal-secret")
    first.write("race/file", "inside")
    race_script = (
        "import os; from pathlib import Path; "
        f"race=Path({str(first.work / 'race')!r}); "
        f"parked=Path({str(first.work / 'parked')!r}); "
        f"target={str(protected)!r}\n"
        "while True:\n"
        " race.rename(parked)\n"
        " race.symlink_to(target, target_is_directory=True)\n"
        " race.unlink()\n"
        " parked.rename(race)\n"
    )
    racer = subprocess.Popen(
        [sys.executable, "-c", race_script],
        user=first.tool_uid,
        group=first.tool_uid,
        extra_groups=[],
        env={},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(30):
            for operation in (
                lambda: first.read("race/file"),
                lambda: first.write("race/file", "inside"),
                lambda: first.edit("race/file", "inside", "inside"),
            ):
                try:
                    output = await asyncio.to_thread(operation)
                    assert "private-journal-secret" not in output
                except ValueError:
                    pass
        assert private.read_text() == "inside-private-journal-secret"
    finally:
        first.stop_processes()
        racer.wait(timeout=2)
    # Escaped descendants must die at command end, timeout and cancellation,
    # with inherited stdout as well as redirected output, in both modes.
    for redirected in (False, True):
        for ending in ("success", "timeout", "cancel"):
            marker = f"child-pid-{redirected}-{ending}"
            suffix = ">/dev/null 2>&1" if redirected else ""
            command = f"setsid sleep infinity {suffix} & echo $! > {marker}"
            if ending != "success":
                command += "; wait"
            first.request.limits.deadline_s = 0.2 if ending == "timeout" else 5
            started = time.monotonic()
            create = asyncio.create_subprocess_exec
            release_spawn = asyncio.Event()

            async def delayed_spawn(
                *args, create=create, ending=ending, release_spawn=release_spawn, **kwargs
            ):
                child = await create(*args, **kwargs)
                if ending == "cancel":
                    await release_spawn.wait()
                return child

            with patch("asyncio.create_subprocess_exec", delayed_spawn):
                task = asyncio.create_task(first.bash(command))
                if ending == "cancel":
                    while True:
                        try:
                            first.read(marker)
                            break
                        except ValueError:
                            await asyncio.sleep(0.01)
                    assert not first.process_groups, "cancel must land during spawn"
                    task.cancel()
                    release_spawn.set()
                try:
                    await asyncio.wait_for(task, 4)
                    assert ending == "success"
                except (TimeoutError, asyncio.CancelledError):
                    assert ending != "success"
            assert time.monotonic() - started < 4
            pid = int(first.read(marker))
            # Probe as the owner: hidepid prevents the service from enumerating it.
            async with asyncio.timeout(1):
                while True:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            "import os,sys; os.kill(int(sys.argv[1]), 0)",
                            str(pid),
                        ],
                        user=first.tool_uid,
                        group=first.tool_uid,
                        extra_groups=[],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                    )
                    if result.returncode != 0:
                        break
                    await asyncio.sleep(0.01)
    if mode == "single":
        await checkpoint_probe(home)
    await app.state.supervisor.close()


if __name__ == "__main__":
    asyncio.run(main())
