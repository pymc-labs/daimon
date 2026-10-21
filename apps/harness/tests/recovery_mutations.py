"""Run nine recovery mutations on isolated source copies (never edit the checkout).

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
for number in [int(value) for value in sys.argv[2:]] or range(1, 11):
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
    elif number == 5:
        change(
            "journal.py",
            "    def mark_diverged(self) -> None:\n        self.diverged = True",
            "    def mark_diverged(self) -> None:\n        self.diverged = False",
        )
    elif number == 6:
        change(
            "service.py",
            "                if workers_stopped:\n",
            "                if True:\n",
        )
    elif number == 7:
        change(
            "checkpoints.py",
            "command.extend((str(user), str(EXPORT_UID)))",
            "command.extend((str(user), str(user)))",
        )
        # UID switching is essential to this mutation. Run the same production
        # overlap probe as CI in a cached image, with the mutated package copied in.
        image = os.environ.get("HARNESS_MUTATION_IMAGE", "daimon-harness:l4-hardening")
        container = subprocess.check_output(
            [
                "docker",
                "create",
                "--init",
                "--entrypoint",
                "/bin/bash",
                image,
                "-c",
                "uv pip install --python /opt/venv/bin/python azure-storage-blob==12.31.0 "
                "zstandard==0.25.0 && exec /opt/venv/bin/python /probe.py",
            ],
            text=True,
        ).strip()
        try:
            for local, remote in (
                (str(target) + "/.", "/opt/venv/lib/python3.12/site-packages/daimon_harness/"),
                ("packages/mux/mux/.", "/opt/venv/lib/python3.12/site-packages/mux/"),
                ("apps/harness/tests/image_probe.py", "/probe.py"),
            ):
                subprocess.run(["docker", "cp", local, f"{container}:{remote}"], check=True)
            with (root / "mutation-7.log").open("w") as output:
                subprocess.run(
                    ["docker", "start", "-a", container], stdout=output, stderr=subprocess.STDOUT
                )
            code = int(
                subprocess.check_output(
                    ["docker", "inspect", "--format", "{{.State.ExitCode}}", container], text=True
                )
            )
            print(f"mutation 7: production image exit {code}", flush=True)
            assert code == 1
            assert (
                "exporter killed by tool cleanup (checkpoint data loss)"
                in (root / "mutation-7.log").read_text()
            )
        finally:
            subprocess.run(["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL)
        continue
    elif number == 8:
        change(
            "kill_uid.py",
            "for pid in present_pids():",
            'for pid in range(2, int(Path("/proc/sys/kernel/ns_last_pid").read_text()) + 1):',
        )
    elif number == 10:
        change(
            "checkpoints.py",
            "            self.reservation_high_water(session_id),",
            "            0,",
        )
    else:
        change(
            "checkpoints.py",
            "(records or remote_seq or after_seq)",
            "(records or remote_seq)",
        )
        change(
            "checkpoints.py",
            'if pending.exists():\n                raise ValueError("observed session has no remote checkpoint")',
            'if pending.exists() or after_seq > 0:\n                raise ValueError("observed session has no remote checkpoint")',
        )
    env = os.environ | {"PYTHONPATH": str(target.parent.resolve())}
    with (root / f"mutation-{number}.log").open("w") as output:
        result = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                (
                    "apps/harness/tests/test_kill_uid.py::test_sparse_four_million_pid_namespace_and_wrap"
                    if number == 8
                    else "apps/harness/tests/test_sequence_ratchet.py::test_sequence_ratchet_random_interleavings"
                ),
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
        6: "I5: poll ended a turn with live workers",
        8: "AssertionError: cleanup scanned the PID allocation range",
        9: "AssertionError: empty remote cursor fence rejected",
        10: "host-seen sequence reused",
    }
    assert expected[number] in (root / f"mutation-{number}.log").read_text()
