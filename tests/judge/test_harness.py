"""Offline grading regressions; never launch a real judge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from . import harness


def recording(task: str = "two_turn_files") -> harness.Transcript:
    return harness.Transcript(
        task_id=task,
        backend="fixture",
        profile="fixture.test",
        recorded_at="2026-10-09T00:00:00Z",
        events=[
            harness.RecordedEvent(
                id="e1", turn=1, speaker="tool", type="file.checksum", payload={"sha256": "fixture"}
            )
        ],
    )


def grade(task: str = "two_turn_files", success: int = 2) -> harness.Grade:
    scores = {
        d: {"score": 2, "evidence": ["e1"], "rationale": "recorded evidence"}
        for d in harness.DIMENSIONS
    }
    scores["task_success"]["score"] = success
    return harness.Grade.model_validate({"task_id": task, "scores": scores})


def test_fixed_tasks_and_rubric() -> None:
    tasks = harness.load_tasks()
    assert len(tasks) == 12 and all(t.prompts for t in tasks.values())
    assert set(json.loads((harness.HERE / "rubric.json").read_text())["dimensions"]) == set(
        harness.DIMENSIONS
    )


@pytest.mark.parametrize(
    "passes,blocked", [([0, 0, 0], True), ([0, 2, 0], False), ([2, 2, 2], False)]
)
def test_gate_requires_all_reps_to_fail(
    monkeypatch: pytest.MonkeyPatch, passes: list[int], blocked: bool
) -> None:
    iterator = iter(passes)

    def fake_judge(
        recording: harness.Transcript, task: harness.Task, *, timeout: float = 300
    ) -> harness.Grade:
        return grade(success=next(iterator))

    monkeypatch.setattr(harness, "judge", fake_judge)
    result = harness.replay([recording()])
    assert result["blocked"] is blocked
    assert result["conformance"] == "reported_separately"
    assert result["scope"] == "supplied_recordings_only"
    backends = result["backends"]
    assert isinstance(backends, list)
    assert backends[0]["complete"] is False and len(backends[0]["missing_tasks"]) == 11
    assert backends[0]["pass_rate"] == sum(p == 2 for p in passes) / 3


def test_evidence_ids_and_task_identity_are_checked() -> None:
    task = harness.load_tasks()["two_turn_files"]
    g = grade()
    g.scores.task_success.evidence = ["invented"]
    with pytest.raises(harness.JudgeError, match="unknown recorded event"):
        harness.validate_grade(g, recording(), task)
    with pytest.raises(harness.JudgeError, match="ID mismatch"):
        harness.validate_grade(grade("repo_edit"), recording(), task)
    with pytest.raises(ValidationError):
        harness.Score(score=2, evidence=[], rationale="unsupported claim")


def test_invalid_runs_do_not_become_task_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*a: object, **k: object) -> None:
        raise harness.JudgeError("invalid output")

    monkeypatch.setattr(harness, "judge", fail)
    with pytest.raises(harness.JudgeError):
        harness.replay([recording()])
    with pytest.raises(ValueError, match="duplicate"):
        harness.replay([recording(), recording()])
    with pytest.raises(ValueError, match="reps"):
        harness.replay([recording()], reps=0)
    with pytest.raises(ValueError, match="unknown"):
        harness.replay([recording("invented")])


def test_subscription_only_and_structured_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text('{"auth_mode":"chatgpt","OPENAI_API_KEY":null}')
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-forwarded")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://must-not-be-forwarded.invalid")
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: float,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert command[:2] == ["codex", "exec"]
        assert command[command.index("--model") + 1] == "gpt-6.1-sol"
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command and "--ephemeral" in command
        assert "OPENAI_API_KEY" not in env and "OPENAI_BASE_URL" not in env
        schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        assert schema["additionalProperties"] is False
        Path(command[command.index("--output-last-message") + 1]).write_text(
            grade().model_dump_json()
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    g = harness.judge(recording(), harness.load_tasks()["two_turn_files"])
    assert g.task_id == "two_turn_files" and len(calls) == 1
    (tmp_path / "auth.json").write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"fixture"}')
    with pytest.raises(harness.JudgeError, match="API-key auth refused"):
        harness.judge(recording(), harness.load_tasks()["two_turn_files"])
    assert len(calls) == 1


def test_subprocess_failure_is_content_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "require_subscription_auth", lambda: None)

    def fail(command: list[str], **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, command, stderr="secret upstream transcript")

    monkeypatch.setattr(harness.subprocess, "run", fail)
    with pytest.raises(harness.JudgeError) as exc:
        harness.judge(recording(), harness.load_tasks()["two_turn_files"])
    assert "secret" not in str(exc.value)
