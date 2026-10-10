from __future__ import annotations

import io
import json
import subprocess
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
    assert calls[0]["temperature"] == 0
    assert calls[0]["output_config"]
    with pytest.raises(Pending, match="budget"):
        judge.evaluate("rubric", "a" * pricing.judge_input_token_limit)
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


def test_alert_delivery_failure_does_not_advance_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake"]), tmp_path / "state.json"
    )
    with pytest.raises(RuntimeError, match="delivery failed"):
        alerts.notify(Result("run", "QA-D1-TEST", "staging", "FAIL"), tmp_path / "run.json")
    assert not alerts.state_path.exists()


def test_report_has_json_evidence_and_five_line_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = Result("run", "QA-D1-TEST", "staging", checks=[Check("text", "PASS", "ok")])
    result.finalize()
    path = report(result, tmp_path)
    assert json.loads(path.read_text())["status"] == "PASS"
    assert len(capsys.readouterr().out.splitlines()) == 5
