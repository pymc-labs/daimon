"""Standalone root-image probe, run in CI without Azure credentials."""

import asyncio
import os
import shlex
import sys
from pathlib import Path

from daimon_harness.isolation import validate_session_mode
from daimon_harness.journal import ConflictError, Journal
from daimon_harness.runtime import TurnContext, discard_event
from daimon_harness.service import create_app
from mux.drivers.selfhosted.wire import InlineSkill, TurnRequest


async def main() -> None:
    assert os.geteuid() == 0
    home = Path(os.environ.get("HARNESS_PROBE_HOME", "/home/harness"))
    secret = "synthetic-runtime-secret"
    os.environ["HARNESS_TOKEN"] = secret
    os.environ["AZURE_OPENAI_API_KEY"] = secret
    app = create_app(home=home)
    mode = validate_session_mode()
    assert os.geteuid() == 10000
    journal: Journal = app.state.supervisor.journal
    contexts: list[TurnContext] = []
    for name in ("first", "second") if mode == "multi" else ("first",):
        sandbox, uid = journal.sandbox(name)
        request = TurnRequest(
            session_id=name,
            turn_id=name,
            idempotency_key=name,
            model="test",
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
        pid_output = await first.bash(
            f"setsid {shlex.quote(sys.executable)} -c {shlex.quote(sleeper)} "
            "private-command-marker >/dev/null 2>&1 & "
            'while [ ! -f "$TMPDIR/ready" ]; do sleep 0.01; done; cat "$TMPDIR/ready"'
        )
        pid = int(pid_output.splitlines()[1])
        try:
            own = await first.bash(f'cat /proc/{pid}/cmdline; cat "$TMPDIR/report"')
            assert "private-command-marker" in own, own
            assert "private-tmp" in own, own
            output = await second.bash(
                f"cat {first.home}/tmp/report /proc/{pid}/cmdline 2>/dev/null; "
                "test -w /tmp && echo global-tmp-writable; echo isolated"
            )
            assert "private-tmp" not in output, output
            assert "private-command-marker" not in output, output
            assert "global-tmp-writable" not in output, output
            assert "isolated" in output, output
        finally:
            os.kill(pid, 9)
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
    await first.bash(
        f"setsid {shlex.quote(sys.executable)} -c {shlex.quote(race_script)} >/dev/null 2>&1 &"
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
    await app.state.supervisor.close()


if __name__ == "__main__":
    asyncio.run(main())
