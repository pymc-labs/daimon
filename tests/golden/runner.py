"""Replay the turn path against the committed offline recordings."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

type Job = tuple[str, str | None]  # (scenario name, production mutation)
TURN_CONTROL_KWARGS = frozenset({"backend", "scope", "session_ref"})

ROOT = Path(__file__).resolve().parents[2]
GOLDENS = Path(__file__).resolve().parent
SCENARIOS = {
    "timer": "packages/core/tests/continuity/test_timers.py::test_a_fired_timer_dispatches_its_note_even_after_newer_messages",
    "env_mount_failure": "packages/core/tests/test_session_update_ops.py::test_env_replacement_clears_the_recorded_env_when_the_add_fails_after_the_delete",
    "plain_turn": "packages/core/tests/turn/test_driver.py::test_run_turn_folds_full_stream_and_returns_terminal_state",
    "tool_use": "packages/core/tests/turn/test_driver_policy_approval.py::test_chat_read_runs_without_a_card",
    "approval_card": "packages/core/tests/turn/test_driver_policy_approval.py::test_chat_write_shows_a_card_and_runs_only_after_confirm",
    "approval_card_discord": "tests/test_posted_controls_equivalence.py::test_confirmation_effects_match_recording[approve-discord]",
    "approval_card_slack": "tests/test_posted_controls_equivalence.py::test_confirmation_effects_match_recording[approve-slack]",
    "cancel_mid_stream": "packages/core/tests/turn/test_driver.py::test_interrupt_mid_consume_posts_user_interrupt_and_ends_clean_on_ack",
    "reconnect": "packages/core/tests/turn/test_driver_hooks.py::test_driver_calls_on_reconnect_on_connection_drop",
    "rate_limit": "packages/core/tests/turn/test_driver_hooks.py::test_driver_calls_on_rate_limited_with_until_before_sleep",
    "mcp_degraded": "packages/core/tests/turn/test_driver.py::test_mcp_failure_then_reply_finalizes_as_success_carrying_the_failure",
    "ceiling": "tests/golden/test_boundary_scenarios.py::test_ceiling_after_http_setup_closes_stream",
    "billing_replay": "packages/core/tests/turn/test_driver_replay_billing.py::test_replayed_call_is_debited_with_the_turns_attribution",
    "cold_discord": "tests/golden/test_boundary_scenarios.py::test_cold_thread_feedback_before_session_create[discord]",
    "cold_slack": "tests/golden/test_boundary_scenarios.py::test_cold_thread_feedback_before_session_create[slack]",
    "dm_delivery": "tests/golden/test_boundary_scenarios.py::test_dm_reply_delivery_and_deduplication",
    "plain_discord": "tests/parity/test_turn_billed.py::test_turn_billed_when_unblocked_writes_usage_event_and_ledger_debit[discord]",
    "plain_slack": "tests/parity/test_turn_billed.py::test_turn_billed_when_unblocked_writes_usage_event_and_ledger_debit[slack]",
    "blocked_balance": "tests/parity/test_turn_blocked_balance.py::test_turn_blocked_when_over_balance_writes_no_usage_and_no_ledger_row[discord]",
    "blocked_cap": "tests/parity/test_turn_blocked_cap.py::test_turn_blocked_when_over_cap_writes_no_new_usage_or_ledger_row[slack]",
    "dead_session": "tests/parity/test_dead_session.py::test_dead_session_recreates_marks_old_row_dead_and_bills_new_session[discord]",
    "handoff_full": "packages/core/tests/test_workspace_transfer.py::test_transfer_rehosts_the_bundle_and_enqueues_its_deletion",
    "handoff_transcript": "packages/core/tests/test_workspace_transfer.py::test_transfer_degrades_to_transcript_when_the_old_session_is_archived",
    "handoff_history": "packages/core/tests/test_workspace_transfer.py::test_transfer_returns_history_only_when_the_session_log_is_gone",
    "wake_continuation": "tests/parity/test_private_input_dispatch.py::test_private_input_continuation_dispatches_exactly_one_follow_up_turn[discord]",
    "handoff_continuation": "tests/parity/test_handoff_dispatch.py::test_handoff_continuation_dispatches_exactly_one_follow_up_turn[discord]",
    "dm_turn": "tests/integration/test_direct_messages.py::test_dm_scope_reuses_session_replays_history_bills_and_deduplicates[discord]",
    "sealed_channel": "packages/adapters/scheduler/tests/test_main.py::test_fire_stamps_a_sealed_destinations_seal_on_the_routine_session",
    "scheduler_run": "packages/adapters/scheduler/tests/test_main.py::test_fire_runs_in_the_routine_channels_environment[channel]",
    "mcp_start_turn": "packages/adapters/mcp/tests/tools/test_agent_chat.py::test_start_turn_returns_the_accepted_events_boundary",
    "mcp_continue_turn": "packages/adapters/mcp/tests/tools/test_agent_chat.py::test_continue_turn_returns_boundary_from_its_own_send",
    "mcp_cancel_turn": "packages/adapters/mcp/tests/tools/test_agent_chat.py::test_cancel_turn_sends_exactly_one_user_interrupt_event",
    "cli_session_get": "packages/adapters/cli/tests/commands/test_sessions.py::test_sessions_get_json_prints_session_body",
}


# Extra child-only probes that ride in the first batch. Their transcripts are not goldens.
PROBES = {
    "controls_probe": "tests/golden/test_turn_path_instrumentation.py::test_controls_are_forwarded_without_erasing_semantic_kwargs",
    **{
        f"uuid_probe_{count}": "tests/golden/test_turn_path_instrumentation.py::"
        f"test_lease_inserts_preserve_host_uuid_sequence[{count}]"
        for count in (0, 1, 3)
    },
}
# Sensitivity probes: the scenario must pass, yet its transcript must differ from the golden.
MUTATIONS: tuple[Job, ...] = (
    ("plain_slack", "slack_eyes"),
    ("cold_discord", "discord_eyes"),
    ("cold_discord", "ledger_dating"),
    ("dm_delivery", "dm_delivery"),
)
SHARDS = 1
MAX_PARALLEL = 4


# Well inside the 15-minute CI job timeout; faulthandler dumps a hung test's stacks first.
BATCH_TIMEOUT = 300
HANG_TRACEBACK_AFTER = 200
LOG_TAIL = 12000


def _child_env(manifest: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["DAIMON_ORACLE_MANIFEST"] = str(manifest)
    env.pop("DAIMON_ORACLE_MUTATION", None)
    env.pop("PYTEST_ADDOPTS", None)  # ambient options must not change a child's selection
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(GOLDENS), str(ROOT / "tests"), env.get("PYTHONPATH", "")))
    )
    return env


def NODEIDS() -> dict[str, str]:
    return {**SCENARIOS, **PROBES}


class Batch:
    """One child pytest process replaying several scenarios.

    The oracle plugin resets its recorder, patches and counters between scenarios, so a
    batch costs one interpreter start and one schema build instead of one per scenario.
    A scenario may appear once per batch; run a second batch for a second mutation.
    """

    def __init__(self, jobs: Sequence[Job], scratch: Path) -> None:
        names = [name for name, _ in jobs]
        if len(set(names)) != len(names):
            raise ValueError("A scenario can appear once per batch")
        self.jobs = list(jobs)
        self.scratch = scratch
        self.out = scratch / "out"
        self.out.mkdir(parents=True)
        entries = [
            {"key": name, "nodeid": NODEIDS()[name], "mutation": mutation}
            for name, mutation in self.jobs
        ]
        manifest = scratch / "manifest.json"
        manifest.write_text(json.dumps({"output_dir": str(self.out), "entries": entries}))
        self.command = [
            sys.executable,
            "-m",
            "pytest",
            *(NODEIDS()[name] for name in names),
            "-p",
            "oracle_plugin",
            "-p",
            "turn_scope_plugin",
            "-q",
            "--tb=short",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "-o",
            f"faulthandler_timeout={HANG_TRACEBACK_AFTER}",
        ]
        self.process = subprocess.Popen(
            self.command,
            cwd=ROOT,
            env=_child_env(manifest),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,  # so a timeout can reap grandchildren too
        )
        self.log = ""
        self.problems: dict[str, str] = {}

    def _stop_group(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except OSError:
            self.process.kill()

    def _read_state(self) -> dict[str, dict[str, Any]] | None:
        try:
            state = json.loads((self.out / "state.json").read_text())
            return {key: dict(state[key]) for key in ("expected", "completed", "failures")}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def wait(self) -> dict[str, str]:
        """Return {scenario: transcript} for each scenario that completed and passed.

        A scenario passes only if the child recorded every one of its items as finished,
        none failed, and its transcript exists. Anything else, including a child that died
        part-way, is a failure kept in `self.problems` with the child log tail.
        """
        timed_out = False
        try:
            self.log, _ = self.process.communicate(timeout=BATCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._stop_group()
            self.log, _ = self.process.communicate()
            self.log += f"\nOracle batch timed out after {BATCH_TIMEOUT}s"
        returncode = self.process.returncode
        state = self._read_state()
        aborted = timed_out or state is None or returncode not in (0, 1)
        reason = "timed out" if timed_out else f"returncode {returncode}"
        tail = f"\n--- child output (tail) ---\n{self.log[-LOG_TAIL:]}"
        transcripts: dict[str, str] = {}
        running_seen = False  # the first unfinished scenario is the one the child died in
        for name, _ in self.jobs:
            expected = state["expected"].get(name, 0) if state else 0
            finished = (
                state is not None and expected > 0 and state["completed"].get(name) == expected
            )
            transcript = self.out / f"{name}.json"
            if state is not None and name in state["failures"]:
                self.problems[name] = str(state["failures"][name])
            elif finished and transcript.exists():
                transcripts[name] = transcript.read_text()
            elif aborted:
                if state is None or running_seen or finished:
                    when = "before running" if not finished else "after running"
                else:
                    when = "while running"
                    running_seen = True
                self.problems[name] = f"child aborted ({reason}) {when} {name}{tail}"
            else:
                self.problems[name] = f"scenario {name} did not complete{tail}"
        return transcripts

    def explain(self, name: str, transcripts: dict[str, str]) -> str:
        """Failure text for one scenario; empty when it completed and passed."""
        if name in transcripts:
            return ""
        return self.problems.get(name) or self.log


def replay_batch(jobs: Sequence[Job]) -> dict[str, str]:
    """Replay several scenarios in one child; raise unless every one produced a transcript."""
    with tempfile.TemporaryDirectory(prefix="daimon-oracle-") as scratch:
        batch = Batch(jobs, Path(scratch))
        transcripts = batch.wait()
        for name, _ in batch.jobs:
            problem = batch.explain(name, transcripts)
            assert not problem and name in transcripts, (
                f"Oracle scenario {name} failed:\n{problem or batch.log}"
            )
        return transcripts


def replay(name: str, *, mutation: str | None = None) -> str:
    """Replay one scenario in its own child (probes, CLI and regeneration of a single golden)."""
    return replay_batch([(name, mutation)])[name]


def job_key(name: str, mutation: str | None) -> str:
    return name if mutation is None else f"{name}@{mutation}"


def plan() -> list[list[Job]]:
    """Every child process needed to check all scenarios and mutation probes."""
    shards: list[list[Job]] = [[] for _ in range(SHARDS)]
    for index, name in enumerate(SCENARIOS):
        shards[index % SHARDS].append((name, None))
    shards[0].extend((name, None) for name in PROBES)
    groups: list[list[Job]] = []
    for job in MUTATIONS:  # a scenario may appear once per child
        group = next((g for g in groups if all(job[0] != other[0] for other in g)), None)
        if group is None:
            groups.append(group := [])
        group.append(job)
    return [*shards, *groups]


class SharedReplays:
    """Replay every scenario once, in parallel children, shared by all pytest workers.

    The first caller (holding a file lock under `root`) starts all children and stores one file
    per job; later callers, including other xdist workers, read the stored results.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _ensure(self) -> None:
        import fcntl
        import shutil
        from concurrent.futures import ThreadPoolExecutor

        with (self.root / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (self.root / "done").exists():
                return
            # A retry must never see results of an earlier attempt.
            shutil.rmtree(self.root / "results", ignore_errors=True)

            def run(jobs: list[Job]) -> None:
                try:
                    with tempfile.TemporaryDirectory(prefix="daimon-oracle-") as scratch:
                        batch = Batch(jobs, Path(scratch))
                        transcripts = batch.wait()
                        for name, mutation in jobs:
                            problem = batch.explain(name, transcripts)
                            self._store(
                                name,
                                mutation,
                                None if problem else transcripts.get(name),
                                problem or batch.log,
                            )
                except Exception as error:  # a broken batch fails only its own jobs
                    for name, mutation in jobs:
                        self._store(name, mutation, None, f"batch failed: {error!r}")

            try:
                with ThreadPoolExecutor(MAX_PARALLEL) as pool:
                    list(pool.map(run, plan()))
            finally:
                # Always mark the run finished, or every later test would repeat the replay.
                (self.root / "done").write_text("ok")

    def _store(self, name: str, mutation: str | None, transcript: str | None, error: str) -> None:
        directory = self.root / "results"
        directory.mkdir(exist_ok=True)
        key = job_key(name, mutation)
        stored, other = ("json", "err") if transcript is not None else ("err", "json")
        (directory / f"{key}.{other}").unlink(missing_ok=True)
        (directory / f"{key}.{stored}").write_text(transcript if transcript is not None else error)

    def transcript(self, name: str, mutation: str | None = None) -> str:
        self._ensure()
        directory = self.root / "results"
        key = job_key(name, mutation)
        if (directory / f"{key}.json").exists():
            return (directory / f"{key}.json").read_text()
        error = directory / f"{key}.err"
        detail = error.read_text() if error.exists() else "no result was recorded"
        raise AssertionError(f"Oracle scenario {key} failed:\n{detail}")


def stable_transcript(transcript: str) -> str:
    """Ignore only the explicitly labelled additive DB-column capture.

    The raw replay retains new fields for binding-specific assertions. Existing
    database fields, effects, requests and every other root section stay exact.
    """
    data = json.loads(transcript)
    if "database_extensions" not in data:
        return transcript
    del data["database_extensions"]
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def compare(name: str, transcript: str, *, regen: bool = False) -> None:
    """Compare one replayed transcript with its stored golden (or rewrite the golden)."""
    actual = stable_transcript(transcript)
    path = GOLDENS / f"{name}.json"
    if regen:
        path.write_text(actual)
        return
    expected = path.read_text()
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(True), actual.splitlines(True), fromfile=str(path), tofile="replay"
        )
    )
    assert actual == expected, f"Golden changed; regenerate only for an intended change.\n{diff}"


def check(name: str, *, regen: bool = False, mutation: str | None = None) -> None:
    if regen and mutation is not None:
        raise ValueError("Only an unmutated replay may regenerate shared golden files")
    compare(name, replay(name, mutation=mutation), regen=regen)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regen", action="store_true", help="overwrite the baseline recordings")
    parser.add_argument("scenarios", nargs="*", choices=tuple(SCENARIOS))
    parser.add_argument(
        "--mutation", choices=("slack_eyes", "discord_eyes", "ledger_dating", "dm_delivery")
    )
    args = parser.parse_args()
    if args.regen and args.mutation:
        parser.error("Only an unmutated replay may regenerate shared golden files")
    names = list(args.scenarios or SCENARIOS)
    # One child replays every requested scenario.
    transcripts = replay_batch([(name, args.mutation) for name in names])
    for name in names:
        compare(name, transcripts[name], regen=args.regen)
        print(f"{name}: {'recorded' if args.regen else 'matched'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
