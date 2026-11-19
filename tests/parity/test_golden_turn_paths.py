"""Oracle replay batching, regeneration guards and offline fixture authorization."""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from daimon.core.turn import driver
from mux.contracts.ids import Scope

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "oracle_paths_runner", ROOT / "tests/golden/runner.py"
)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


async def bridged_turn(
    *, scope: Scope | None = None, backend: object = None, session_ref: object = None, **kwargs: Any
) -> dict[str, Any]:
    return dict(scope=scope, backend=backend, session_ref=session_ref, **kwargs)


def test_replay_compares_one_shared_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(RUNNER, "GOLDENS", tmp_path)
    baseline = json.dumps({"price": "0.0000001234", "caller": "literal"}) + "\n"
    golden = tmp_path / "plain_turn.json"
    golden.write_text(baseline)
    changed = False

    class ReplayChild:
        """Stands in for the child pytest: writes the transcript the oracle plugin would."""

        def __init__(self, command: list[str], *, env: dict[str, str], **kwargs: object) -> None:
            assert "oracle_plugin" in command and "turn_scope_plugin" in command
            manifest = json.loads(Path(env["DAIMON_ORACLE_MANIFEST"]).read_text())
            assert [entry["key"] for entry in manifest["entries"]] == ["plain_turn"]
            self.output_dir = Path(manifest["output_dir"])
            Path(manifest["output_dir"], "plain_turn.json").write_text(
                baseline.replace("0.0000001234", "0.0000001235") if changed else baseline
            )

        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, None]:
            write_child_state(
                self.output_dir, expected={"plain_turn": 1}, completed={"plain_turn": 1}
            )
            return "", None

    monkeypatch.setattr(subprocess, "Popen", ReplayChild)
    RUNNER.check("plain_turn")
    changed = True
    with pytest.raises(AssertionError, match="Golden changed"):
        RUNNER.check("plain_turn")
    assert list(tmp_path.glob("*.json")) == [golden]
    assert golden.read_text() == baseline


def write_child_state(
    output_dir: Path,
    *,
    expected: dict[str, int],
    completed: dict[str, int],
    failures: dict[str, str] | None = None,
) -> None:
    """What oracle_plugin.write_state leaves behind after each report."""
    (output_dir / "state.json").write_text(
        json.dumps({"expected": expected, "completed": completed, "failures": failures or {}})
    )


def fake_child(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int,
    transcripts: tuple[str, ...] = (),
    expected: dict[str, int] | None = None,
    completed: dict[str, int] | None = None,
    failures: dict[str, str] | None = None,
    log: str = "child log line\n",
    spawned: list[list[str]] | None = None,
) -> None:
    """Replace the child pytest with one that leaves exactly this on disk, then exits."""

    class Child:
        def __init__(self, command: list[str], *, env: dict[str, str], **kwargs: object) -> None:
            if spawned is not None:
                spawned.append(command)
            manifest = json.loads(Path(env["DAIMON_ORACLE_MANIFEST"]).read_text())
            self.out = Path(manifest["output_dir"])
            self.returncode: int | None = None
            for name in transcripts:
                (self.out / f"{name}.json").write_text("{}\n")
            if expected is not None:
                write_child_state(
                    self.out, expected=expected, completed=completed or {}, failures=failures
                )

        def communicate(self, timeout: float | None = None) -> tuple[str, None]:
            self.returncode = returncode
            return log, None

    monkeypatch.setattr(subprocess, "Popen", Child)


@pytest.mark.parametrize(
    ("completed", "failures", "expect_recorded"),
    (
        # plain_turn's teardown failed (recorded), then the child was killed in tool_use.
        ({"plain_turn": 1}, {"plain_turn": "TEARDOWN BOOM"}, "TEARDOWN BOOM"),
        # Killed during plain_turn's own teardown: transcript written, no pass was recorded.
        ({}, {}, "child aborted (returncode -9) while running plain_turn"),
    ),
)
def test_killed_child_cannot_turn_a_failure_or_unfinished_scenario_into_a_pass(
    monkeypatch: pytest.MonkeyPatch,
    completed: dict[str, int],
    failures: dict[str, str],
    expect_recorded: str,
) -> None:
    fake_child(
        monkeypatch,
        returncode=-9,
        transcripts=("plain_turn",),
        expected={"plain_turn": 1, "tool_use": 1},
        completed=completed,
        failures=failures,
        log="LAST CHILD LINE\n",
    )
    with pytest.raises(AssertionError, match="Oracle scenario plain_turn") as caught:
        RUNNER.replay_batch([("plain_turn", None), ("tool_use", None)])
    assert expect_recorded in str(caught.value)
    with tempfile.TemporaryDirectory() as scratch:
        batch = RUNNER.Batch([("plain_turn", None), ("tool_use", None)], Path(scratch))
        assert batch.wait() == {}
        assert expect_recorded in batch.problems["plain_turn"]
        later = batch.problems["tool_use"]
        assert "child aborted (returncode -9)" in later and "tool_use" in later
        assert "LAST CHILD LINE" in later


def test_child_that_dies_before_collecting_reports_it_never_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_child(monkeypatch, returncode=2, log="ImportError: no module\n")
    with pytest.raises(AssertionError, match=r"child aborted \(returncode 2\) before running"):
        RUNNER.replay_batch([("plain_turn", None)])


def test_pytest_failure_exit_still_reports_only_the_failed_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_child(
        monkeypatch,
        returncode=1,
        transcripts=("plain_turn", "tool_use"),
        expected={"plain_turn": 1, "tool_use": 1},
        completed={"plain_turn": 1, "tool_use": 1},
        failures={"tool_use": "TOOL USE BOOM"},
    )
    with tempfile.TemporaryDirectory() as scratch:
        batch = RUNNER.Batch([("plain_turn", None), ("tool_use", None)], Path(scratch))
        assert list(batch.wait()) == ["plain_turn"]
        assert batch.problems == {"tool_use": "TOOL USE BOOM"}


def test_child_environment_drops_ambient_pytest_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing")
    assert "PYTEST_ADDOPTS" not in RUNNER._child_env(Path("manifest.json"))


@pytest.fixture
def one_batch_replays(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setattr(RUNNER, "plan", lambda: [[("plain_turn", None)]])
    return RUNNER.SharedReplays(tmp_path / "replays")


def test_stale_transcript_cannot_beat_a_fresh_failure_on_retry(
    monkeypatch: pytest.MonkeyPatch, one_batch_replays: Any
) -> None:
    stale = one_batch_replays.root / "results"
    stale.mkdir()
    (stale / "plain_turn.json").write_text("STALE PASS\n")
    fake_child(
        monkeypatch,
        returncode=1,
        transcripts=("plain_turn",),
        expected={"plain_turn": 1},
        completed={"plain_turn": 1},
        failures={"plain_turn": "FRESH FAILURE"},
    )
    with pytest.raises(AssertionError, match="FRESH FAILURE"):
        one_batch_replays.transcript("plain_turn")


def test_store_replaces_the_other_suffix(one_batch_replays: Any) -> None:
    one_batch_replays._store("plain_turn", None, "PASS\n", "")
    one_batch_replays._store("plain_turn", None, None, "FAIL")
    directory = one_batch_replays.root / "results"
    assert not (directory / "plain_turn.json").exists()
    assert (directory / "plain_turn.err").read_text() == "FAIL"
    one_batch_replays._store("plain_turn", None, "PASS\n", "")
    assert not (directory / "plain_turn.err").exists()


def test_a_crashing_batch_fails_its_jobs_and_is_not_replayed_again(
    monkeypatch: pytest.MonkeyPatch, one_batch_replays: Any
) -> None:
    attempts: list[object] = []

    class Broken:
        def __init__(self, *args: object, **kwargs: object) -> None:
            attempts.append(args)

        def wait(self) -> dict[str, str]:
            raise OSError("cannot read results")

    monkeypatch.setattr(RUNNER, "Batch", Broken)
    for _ in range(2):
        with pytest.raises(AssertionError, match="cannot read results"):
            one_batch_replays.transcript("plain_turn")
    assert len(attempts) == 1
    assert (one_batch_replays.root / "done").exists()


def test_regeneration_guard_runs_before_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("forbidden regeneration must not start a replay")

    monkeypatch.setattr(RUNNER, "replay", unexpected)
    with pytest.raises(ValueError, match="unmutated replay"):
        RUNNER.check("plain_turn", mutation="slack_eyes", regen=True)


@pytest.fixture
def scope_plugin(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setattr(sys, "path", [str(ROOT / "tests/golden"), *sys.path])
    return importlib.import_module("turn_scope_plugin")


async def test_fixture_injects_the_offline_scope_into_real_aliases(
    monkeypatch: pytest.MonkeyPatch, scope_plugin: ModuleType
) -> None:
    monkeypatch.setattr(driver, "run_turn", bridged_turn)
    source = ModuleType("offline_source")
    vars(source)["run_turn"] = bridged_turn
    host = ModuleType("daimon.offline_fixture_host")
    vars(host)["imported_alias"] = bridged_turn
    monkeypatch.setitem(sys.modules, host.__name__, host)
    anthropic_turn = ModuleType("mux.drivers.anthropic.turn")
    vars(anthropic_turn)["datetime"] = None
    monkeypatch.setitem(sys.modules, anthropic_turn.__name__, anthropic_turn)
    scope_plugin.install_turn_scope(source=source, patch=monkeypatch, clock=datetime)
    result = await source.run_turn(session_id="sess_literal")
    assert result["session_id"] == "sess_literal"
    assert host.imported_alias is source.run_turn is driver.run_turn
    assert result["scope"] == Scope(
        tenant_id="offline-oracle-tenant",
        account_id="offline-oracle-account",
        principal_id="offline-oracle-host",
        authorization_id="offline-scripted-turn",
    )
    assert anthropic_turn.datetime is datetime
    caller_scope = Scope(
        tenant_id="actual-tenant",
        account_id="actual-account",
        principal_id="actual-caller",
        authorization_id="actual-authorization",
    )
    backend, session_ref = object(), object()
    actual = await host.imported_alias(scope=caller_scope, backend=backend, session_ref=session_ref)
    assert actual["scope"] is caller_scope
    assert actual["backend"] is backend
    assert actual["session_ref"] is session_ref


def test_actual_mock_recorder_omits_only_control_kwargs(golden_replays: Any) -> None:
    data = json.loads(golden_replays.transcript("controls_probe"))
    effects = [effect for effect in data["effects"] if effect["operation"] == "run_turn"]
    assert len(effects) == 1
    kwargs = effects[0]["payload"]["kwargs"]
    assert kwargs == {
        "session_id": "<id:1>",
        "user_message": "Literal /notes/file_alpha",
        "model_id": "caller-model",
        "price": "0.0000001234",
        "continuity": "history",
    }


@pytest.mark.parametrize("lease_count", (0, 1, 3))
def test_actual_oracle_isolates_neutral_state_uuid_defaults(
    golden_replays: Any, lease_count: int
) -> None:
    # Child assertions exercise mapped defaults and real Postgres lease inserts.
    assert golden_replays.transcript(f"uuid_probe_{lease_count}")


def test_cli_replays_every_scenario_in_one_child(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["runner.py"])
    seen: list[str] = []
    batches: list[int] = []

    def replay_batch(jobs: list[tuple[str, str | None]]) -> dict[str, str]:
        batches.append(len(jobs))
        return {name: "" for name, _ in jobs}

    monkeypatch.setattr(RUNNER, "replay_batch", replay_batch)
    monkeypatch.setattr(RUNNER, "compare", lambda name, transcript, **_: seen.append(name))
    assert RUNNER.main() == 0
    assert batches == [len(RUNNER.SCENARIOS)]
    assert sorted(seen) == sorted(RUNNER.SCENARIOS)
    assert capsys.readouterr().out.count(": matched") == len(RUNNER.SCENARIOS)
