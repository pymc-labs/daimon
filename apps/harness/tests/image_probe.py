"""Standalone root-image probe, run in CI without Azure credentials."""

import asyncio
import os
import shlex
import sys
from pathlib import Path

from daimon_harness.journal import Journal
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
    assert os.geteuid() == 10000
    journal: Journal = app.state.supervisor.journal
    contexts: list[TurnContext] = []
    for name in ("first", "second"):
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
    first, second = contexts
    output = await first.bash(
        f"env; cat /proc/{os.getpid()}/environ 2>/dev/null; "
        f"cat /proc/{os.getpid()}/cmdline 2>/dev/null; "
        f"cat {second.work}/file.txt 2>/dev/null; "
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
