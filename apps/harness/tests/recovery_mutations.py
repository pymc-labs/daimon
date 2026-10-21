"""Run five recovery mutations on isolated source copies (never edit the checkout).

Usage: uv run python apps/harness/tests/recovery_mutations.py <empty scratch directory>
Run from the repository root; each property-test failure is retained in scratch.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
source = Path("apps/harness/src/daimon_harness")
for number in range(1, 6):
    target = root / f"mutation-{number}" / "daimon_harness"
    shutil.copytree(source, target)

    def change(filename, before, after, target=target, number=number):
        path = target / filename
        text = path.read_text()
        assert before in text, (number, filename)
        path.write_text(text.replace(before, after, 1))

    if number == 1:
        change(
            "checkpoints.py",
            "        if cursor_file.exists():\n",
            "        if journal.turns() and not pending.exists() and not cursor_file.exists():\n            return journal\n        if cursor_file.exists():\n",
        )
        change(
            "service.py",
            "            if supervisor.checkpoints is None:\n                await supervisor.io(supervisor.recover)",
            "            await supervisor.io(supervisor.recover)",
        )
    elif number == 2:
        change(
            "service.py", "            await self.io(self.deliver_checkpoint_notice, request)\n", ""
        )
        change(
            "service.py",
            "            if await self.io(self.journal.claim, request):\n",
            "            if await self.io(self.journal.claim, request):\n                await self.io(self.deliver_checkpoint_notice, request)\n",
        )
    elif number == 3:
        path = target / "journal.py"
        text = path.read_text()
        start = text.index('            if kind == "usage":\n', text.index("    def append("))
        end = text.index("            raise\n", start)
        path.write_text(text[:start] + text[end:])
    elif number == 4:
        for filename in ("journal.py", "checkpoints.py"):
            change(
                filename,
                '{"model_request_started", "usage", "turn_ended"}',
                '{"model_request_started", "usage"}',
            )
    else:
        change(
            "journal.py",
            "    def mark_diverged(self) -> None:\n        self.diverged = True",
            "    def mark_diverged(self) -> None:\n        self.diverged = False",
        )
    env = os.environ | {"PYTHONPATH": str(target.parent.resolve())}
    with (root / f"mutation-{number}.log").open("w") as output:
        result = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                "apps/harness/tests/test_sequence_ratchet.py::test_sequence_ratchet_random_interleavings",
                "-xq",
                "--hypothesis-seed=7",
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    print(f"mutation {number}: pytest exit {result.returncode}", flush=True)
    assert result.returncode == 1
    expected = {
        1: "measured became unknown",
        2: "warning submit was not accepted",
        3: "measured became unknown",
        4: "event after terminal",
        5: "measured became unknown",
    }
    assert expected[number] in (root / f"mutation-{number}.log").read_text()
