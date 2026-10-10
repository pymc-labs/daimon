from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from qa.live.config import Alerts, Config, Pricing, Target
from qa.live.cost import Ledger
from qa.live.discord import DiscordBackend
from qa.live.model_probe import ModelEvidence
from qa.live.models import STAGING_LEGACY_MODEL
from qa.live.report import Alerter, Result, report
from qa.live.runner import Executor
from qa.live.schema import Scenario
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.tests.test_discord import FakeDriver
from qa.live.types import Check


def test_execution_trace_is_private_redacted_and_pending(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
    tmp_path: Path,
) -> None:
    credential = "offline-sensitive-key-123"
    monkeypatch.setenv("ANTHROPIC_API_KEY", credential)

    def broken(channel: str) -> None:
        raise ValueError(f"probe failed with {credential}")

    monkeypatch.setattr(backend, "verify_model", broken)
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING" and backend.sent == 0
    assert result.checks[0].reason == "harness error: ValueError"
    assert result.errors[0]["type"] == "ValueError"
    assert result.errors[0]["message"] == "probe failed with [redacted]"
    assert result.errors[0]["frames"]
    assert "broken" in str(result.errors[0]["traceback"])
    assert credential not in report(result, tmp_path).read_text()
    assert json.loads(ledger.path.read_text())["usd"] == 0


def test_model_probe_retries_transient_transport_but_not_wrong_model(
    monkeypatch: pytest.MonkeyPatch,
    pricing: Pricing,
) -> None:
    target = Target(enabled=True, qa_agent_name="haiku-qa", model_probe=["fake-probe"])
    backend = DiscordBackend(
        Config(pricing=pricing, staging=target), "staging", driver=FakeDriver()
    )
    backend.owned.add("parent")
    evidence = ModelEvidence(
        guild_id=target.guild_id,
        channel_id="parent",
        agent_name="haiku-qa",
        agent_id="agent-qa",
        model=STAGING_LEGACY_MODEL,
        channel_pinned=True,
    )
    replies = [
        subprocess.CompletedProcess(["fake-probe"], 1, "", "IAP connection interrupted"),
        subprocess.CompletedProcess(["fake-probe"], 0, evidence.model_dump_json(), ""),
    ]
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return replies.pop(0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("qa.live.discord.time.sleep", lambda _: None)
    backend.verify_model("parent")
    assert len(calls) == 2
    wrong = evidence.model_copy(update={"model": "claude-sonnet-4-5"})
    replies.append(subprocess.CompletedProcess(["fake-probe"], 0, wrong.model_dump_json(), ""))
    with pytest.raises(ValueError, match="cheap-model"):
        backend.verify_model("parent")
    assert len(calls) == 3


def test_harness_pending_never_alerts_but_product_failure_still_does(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake"]), tmp_path / "state.json"
    )
    result = Result(
        "run",
        "QA-D1-TEST",
        "staging",
        "PENDING",
        [Check("execution", "PENDING", "harness error: ValueError")],
    )
    for _ in range(6):
        alerts.notify(result, tmp_path / "run.json")
    assert not calls and not alerts.state_path.exists()
    result.checks.append(Check("text_present", "FAIL", "missing expected product answer"))
    result.finalize()
    alerts.notify(result, tmp_path / "run.json")
    assert len(calls) == 1
