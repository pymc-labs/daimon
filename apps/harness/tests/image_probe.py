"""Standalone root-image probe, run in CI without Azure credentials."""

import asyncio
import os
from pathlib import Path

from daimon_harness.journal import Journal
from daimon_harness.runtime import TurnContext, discard_event
from daimon_harness.service import create_app
from mux.drivers.selfhosted.wire import InlineSkill, TurnRequest


async def main() -> None:
    assert os.geteuid() == 0
    home = Path("/home/harness")
    secret = "synthetic-runtime-secret"
    os.environ["HARNESS_TOKEN"] = secret
    os.environ["AZURE_OPENAI_API_KEY"] = secret
    app = create_app(home=home)
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
        f"cat {first.home}/skills/sample/SKILL.md; id -u"
    )
    assert secret not in output, output
    assert "second" not in output, output
    assert "journal-readable" not in output, output
    assert "journal-writable" not in output, output
    assert "skills-writable" not in output, output
    assert "readable skill" in output, output
    assert "10001" in output, output
    assert await first.bash("cat file.txt") == "exit_code=0\nfirst"
    await app.state.supervisor.close()


if __name__ == "__main__":
    asyncio.run(main())
