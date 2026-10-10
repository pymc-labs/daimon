from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

from qa.live.config import Alerts, Pricing
from qa.live.judge import HaikuJudge
from qa.live.models import ModelPolicy
from qa.live.report import Alerter, Result, report
from qa.live.types import Check, Pending, utcnow

JUDGE_MODEL = ModelPolicy().backends["anthropic"].primary


def test_judge_needs_go_and_exact_model(pricing: Pricing) -> None:
    with pytest.raises(ValueError):
        HaikuJudge(
            pricing,
            go=True,
            models=ModelPolicy.model_validate(
                {"backends": {"anthropic": {"primary": "claude-opus-4-6"}}}
            ),
        )
    with pytest.raises(Pending, match="GO"):
        HaikuJudge(pricing, go=False).evaluate("rubric", "answer")


@pytest.mark.parametrize("response_model", [JUDGE_MODEL, JUDGE_MODEL + "-20261001"])
def test_fixed_judge_request_and_actual_usage(
    response_model: str, monkeypatch: pytest.MonkeyPatch, pricing: Pricing
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-offline-test-key")
    calls: list[dict[str, object]] = []

    def fake_open(request: urllib.request.Request, timeout: int) -> io.BytesIO:
        assert request.full_url == "https://api.anthropic.com/v1/messages"
        calls.append(json.loads(request.data or b"{}"))
        return io.BytesIO(
            json.dumps(
                {
                    "model": response_model,
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": '{"pass":true,"reason":"matches"}'}],
                    "usage": {"input_tokens": 80, "output_tokens": 20},
                }
            ).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    judge = HaikuJudge(pricing, go=True)
    assert judge.evaluate("contains APPLE", "APPLE") == (True, "matches")
    assert judge.usage[0].usd == pytest.approx(0.00018)
    assert judge.usage[0].models == [response_model]
    assert calls[0]["model"] == JUDGE_MODEL
    assert calls[0]["max_tokens"] == 300
    assert "temperature" not in calls[0]  # Haiku 5.5 rejects this deprecated option.
    assert calls[0]["output_config"]
    with pytest.raises(Pending, match="budget"):
        judge.evaluate("rubric", "a" * pricing.judge_input_token_limit)
    assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 503])
def test_judge_http_errors_are_pending_without_retry(
    status: int, monkeypatch: pytest.MonkeyPatch, pricing: Pricing
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-offline-test-key")
    calls: list[str] = []

    def failed_open(request: urllib.request.Request, timeout: int) -> io.BytesIO:
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, status, "secret provider body", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", failed_open)
    judge = HaikuJudge(pricing, go=True)
    with pytest.raises(Pending, match=f"^judge execution unavailable: HTTP {status}$"):
        judge.evaluate("rubric", "answer")
    assert len(calls) == 1
    if status == 400:
        assert not judge.usage
    else:
        assert judge.usage[0].usd is None
        assert judge.usage[0].models == [JUDGE_MODEL]


def test_judge_execution_pending_counts_toward_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake-tsend", "%618"]),
        tmp_path / "state.json",
    )
    result = Result(
        "run",
        "QA-D1-TEST",
        "staging",
        "PENDING",
        [
            Check("text_absent", "PASS", "correct product answer"),
            Check("judge", "PENDING", "judge execution unavailable: HTTP 400"),
        ],
    )
    for _ in range(2):
        alerts.notify(result, tmp_path / "run.json")
    assert not calls
    assert not (tmp_path / "inbox").exists()
    alerts.notify(result, tmp_path / "run.json")
    assert len(calls) == 1
    assert "PENDING" in calls[0][-1]
    result.scenario = "QA-D1-PRODUCT-VERDICT"
    result.checks[-1] = Check("judge", "FAIL", "incorrect answer")
    result.finalize()
    alerts.notify(result, tmp_path / "run.json")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "reason",
    [
        "judge returned another model or incomplete output",
        "ANTHROPIC_API_KEY is unavailable",
        "judge requires the driver's GO",
        "judge input exceeds reserved token budget",
        "turn was not executed",
    ],
)
def test_judge_policy_refusals_alert_after_three_pending_runs(
    reason: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake"]), tmp_path / "state.json"
    )
    result = Result("run", "QA-D1-TEST", "staging", "PENDING", [Check("judge", "PENDING", reason)])
    alerts.notify(result, tmp_path / "run.json")
    alerts.notify(result, tmp_path / "run.json")
    assert not calls
    alerts.notify(result, tmp_path / "run.json")
    assert len(calls) == 1


def test_judge_execution_pending_preserves_other_evidence_streak(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake"]), tmp_path / "state.json"
    )
    result = Result(
        "run",
        "QA-D1-TEST",
        "staging",
        "PENDING",
        [Check("log_absent", "PENDING", "logs unavailable")],
    )
    alerts.notify(result, tmp_path / "run.json")
    alerts.notify(result, tmp_path / "run.json")
    result.checks = [Check("judge", "PENDING", "judge execution unavailable: TypeError")]
    alerts.notify(result, tmp_path / "run.json")
    assert json.loads(alerts.state_path.read_text())["staging:QA-D1-TEST"]["pending_count"] == "3"
    assert len(calls) == 1
    result.checks = [Check("log_absent", "PENDING", "logs unavailable")]
    alerts.notify(result, tmp_path / "run.json")
    assert len(calls) == 1


def test_alert_fail_dedupe_recovery_and_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake-tsend", "%618"]),
        tmp_path / "state.json",
    )
    result = Result(
        "run",
        "QA-D1-TEST",
        "staging",
        "FAIL",
        [
            Check("text_present", "FAIL", "missing APPLE", 1, ["https://discord.com/evidence"]),
        ],
    )
    evidence = tmp_path / "run.json"
    alerts.notify(result, evidence)
    alerts.notify(result, evidence)
    assert len(calls) == 1
    failure = next((tmp_path / "inbox").glob("qa-canary-FAIL-*.md"))
    assert "text_present" in failure.read_text()
    assert "https://discord.com/evidence" in failure.read_text()
    result.status = "PENDING"
    alerts.notify(result, evidence)
    assert len(calls) == 1
    result.status = "FAIL"
    state = json.loads(alerts.state_path.read_text())
    state["staging:QA-D1-TEST"]["ts"] = (utcnow() - timedelta(hours=7)).isoformat()
    alerts.state_path.write_text(json.dumps(state))
    alerts.notify(result, evidence)
    assert len(calls) == 2
    result.status = "PASS"
    alerts.notify(result, evidence)
    alerts.notify(result, evidence)
    assert len(calls) == 3
    assert "RECOVERY" in calls[-1][-1]


def test_queued_alert_advances_durable_inbox_dedupe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake"]), tmp_path / "state.json"
    )
    result = Result("run", "QA-D1-TEST", "staging", "FAIL")
    alerts.notify(result, tmp_path / "run.json")
    alerts.notify(result, tmp_path / "run.json")
    state = json.loads(alerts.state_path.read_text())["staging:QA-D1-TEST"]
    assert state["status"] == "FAIL"
    assert "tsend exit 1" in state["delivery"]
    files = list((tmp_path / "inbox").glob("qa-canary-FAIL-*.md"))
    assert len(files) == 1
    assert "delivered-pending" in files[0].read_text()


def test_report_has_json_evidence_and_five_line_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = Result("run", "QA-D1-TEST", "staging", checks=[Check("text", "PASS", "ok")])
    result.finalize()
    path = report(result, tmp_path)
    assert json.loads(path.read_text())["status"] == "PASS"
    assert len(capsys.readouterr().out.splitlines()) == 5


@pytest.mark.parametrize("error", [OSError("offline"), subprocess.TimeoutExpired("fake", 30)])
def test_alert_command_exception_does_not_halt_or_duplicate(
    error: Exception, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(subprocess, "run", failed)
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake"]), tmp_path / "state.json"
    )
    result = Result("run", "QA-D1-TEST", "staging", "FAIL")
    alerts.notify(result, tmp_path / "run.json")
    alerts.notify(result, tmp_path / "run.json")
    assert len(list((tmp_path / "inbox").glob("*.md"))) == 1
    assert type(error).__name__ in alerts.state_path.read_text()


def test_catalog_continues_after_alert_inbox_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    from qa.live import __main__ as cli
    from qa.live.config import Config, Pricing, Target
    from qa.live.schema import Scenario
    from qa.live.tests.conftest import FakeBackend, FakeJudge

    config = Config(
        pricing=Pricing(per_turn_usd=0.01, judge_input_per_million=1, judge_output_per_million=5),
        staging=Target(enabled=True),
    )
    scenarios = [
        Scenario.model_validate(
            {
                "id": f"QA-D1-TEST{i}",
                "title": "offline",
                "friction": [],
                "sources": [],
                "set": "A",
                "surface": "discord",
                "tier": "full",
                "priority": "P1",
                "est_turns": 1,
                "steps": [{"do": "mention", "text": "APPLE"}, {"do": "wait_done"}],
                "assert": [{"kind": "text_present", "turn": 1, "pattern": "APPLE"}],
            }
        )
        for i in (1, 2)
    ]

    class CLIBackend(FakeBackend):
        target = config.staging

    calls: list[str] = []

    def inbox_unavailable(self: Alerter, result: Result, path: Path) -> None:
        calls.append(result.scenario)
        raise OSError("offline inbox unavailable")

    monkeypatch.setattr(cli, "load_catalog", lambda path: scenarios)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "DiscordBackend", lambda *a, **kw: CLIBackend())
    monkeypatch.setattr(cli, "HaikuJudge", lambda *a, **kw: FakeJudge())
    monkeypatch.setattr(Alerter, "notify", inbox_unavailable)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qa",
            "run",
            "--go",
            "--ledger",
            str(tmp_path / "ledger"),
            "--results",
            str(tmp_path / "results"),
        ],
    )
    assert cli.main() == 0
    assert calls == ["QA-D1-TEST1", "QA-D1-TEST2"]
