import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from qa.live.config import Alerts, Config, Pricing
from qa.live.cost import Ledger
from qa.live.deployment import run_with_deploy_retry, wait_for_stable_deployment
from qa.live.discord import DiscordBackend
from qa.live.report import Alerter, Result
from qa.live.runner import Executor
from qa.live.schema import Scenario
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Check, DeploymentEvidence, Message, Pending, Turn, utcnow


class RollingBackend(FakeBackend):
    def __init__(self, *, changed: bool = False, restart: bool = False) -> None:
        super().__init__()
        self.probes = 0
        self.changed = changed
        self.restart = restart

    def deployment_image(self) -> str:
        self.probes += 1
        return ("b" if self.changed and self.probes > 1 else "a") * 40

    def collect(self, turn: Turn, timeout: float) -> None:
        super().collect(turn, timeout)
        if self.restart:
            turn.messages = [
                {
                    "id": "card",
                    "embeds": [
                        {
                            "title": "Daimon restarted before this request finished.",
                            "color": 15158332,
                        }
                    ],
                }
            ]
            turn.verdicts = ["other_embed"]


@pytest.mark.parametrize("changed,restart", [(True, False), (False, True)])
def test_changed_image_or_restart_preserves_checks_but_never_product_fails(
    changed: bool, restart: bool, scenario: Scenario, ledger: Ledger, pricing: Pricing
) -> None:
    backend = RollingBackend(changed=changed, restart=restart)
    backend.verdict = "error"
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING"
    assert result.deployment and result.deployment.interrupted
    assert result.deployment.start_image == "a" * 40
    assert result.deployment.end_image == ("b" if changed else "a") * 40
    assert any(check.status == "FAIL" for check in result.checks)
    assert any(check.reason == "deploy-interrupted" for check in result.checks)
    if restart:
        assert result.deployment.events[0]["event"] == "restart_card"
    assert backend.events[-1] == "delete"
    assert ledger.charged({result.run_id}) == 0.04


def test_stable_deployment_keeps_real_product_failure(
    scenario: Scenario, ledger: Ledger, pricing: Pricing
) -> None:
    backend = RollingBackend()
    backend.verdict = "error"
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "FAIL"
    assert result.deployment and not result.deployment.interrupted


def test_interrupted_runs_never_alert_or_change_pending_streak(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    prior = {"staging:QA-TEST": {"pending_count": "2", "status": "FAIL"}}
    state.write_text(json.dumps(prior))
    alerter = Alerter(Alerts(inbox=str(tmp_path / "alerts"), command=[]), state)
    result = Result(
        "interrupted", "QA-TEST", "staging", deployment=DeploymentEvidence(interrupted=True)
    )
    result.checks = [Check("watch", "FAIL", "timeout")]
    result.finalize()
    for _ in range(4):
        alerter.notify(result, tmp_path / "result.json")
    assert json.loads(state.read_text()) == prior
    assert not (tmp_path / "alerts").exists()


@pytest.mark.parametrize("twice,allowed", [(False, True), (True, True), (False, False)])
def test_retry_waits_for_stability_is_fresh_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    twice: bool,
    allowed: bool,
    scenario: Scenario,
    ledger: Ledger,
    pricing: Pricing,
) -> None:
    now = [0.0]
    monkeypatch.setattr("qa.live.deployment.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "qa.live.deployment.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    instances: list[RollingBackend] = []
    persisted: list[str] = []

    def factory() -> Executor:
        if instances:
            assert now[0] >= 30
            assert persisted == [first_id[0]]
        backend = RollingBackend(changed=not instances or twice)
        instances.append(backend)
        return Executor(backend, FakeJudge(), ledger, pricing, "staging")

    first_id: list[str] = []

    def persist(result: Result) -> None:
        if not first_id:
            first_id.append(result.run_id)
        persisted.append(result.run_id)

    results = run_with_deploy_retry(scenario, factory, persist, retry_allowed=lambda: allowed)
    assert len(instances) == (2 if allowed else 1)
    assert results[0].status == "PENDING"
    if allowed:
        assert results[1].retry_of == results[0].run_id
        assert results[1].status == ("PENDING" if twice else "PASS")
        assert len(persisted) == 2
    else:
        assert "budget" in results[0].notes[-1]


def test_mixed_workers_cannot_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr("qa.live.deployment.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "qa.live.deployment.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    backend = FakeBackend()

    def unavailable() -> str:
        raise Pending("mixed workers")

    monkeypatch.setattr(backend, "deployment_image", unavailable)
    with pytest.raises(Pending, match="did not settle"):
        wait_for_stable_deployment(backend, timeout_s=35)


def test_probe_requires_all_worker_images_and_full_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.model_validate_json(Path("qa/live/config.example.json").read_text())
    config.staging.enabled = True
    config.staging.deployment_probe = ["readonly-probe"]
    backend = DiscordBackend(config, "staging")
    sha = "a" * 40
    workers = {f"daimon-{name}-1": sha for name in ("discord", "slack", "teams", "scheduler")}
    response = subprocess.CompletedProcess(
        ["readonly-probe"], 0, json.dumps({"image": sha, "workers": workers}), ""
    )

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["readonly-probe"]
        assert json.loads(str(kwargs["input"])) == {
            "env": "staging",
            "guild_id": config.staging.guild_id,
            "read_only": True,
        }
        return response

    monkeypatch.setattr(subprocess, "run", run)
    assert backend.deployment_image() == sha
    workers["daimon-slack-1"] = "b" * 40
    response.stdout = json.dumps({"image": sha, "workers": workers})
    with pytest.raises(Pending, match="mixed"):
        backend.deployment_image()


def test_restart_logs_are_scoped_to_turn_and_active_window(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.model_validate_json(Path("qa/live/config.example.json").read_text())
    config.staging.enabled = True
    backend = DiscordBackend(config, "staging")
    end = utcnow() - timedelta(seconds=60)
    start = end - timedelta(seconds=100)
    turn = Turn(1, "trigger", "parent", start, ended_at=end - timedelta(seconds=10))
    turn.messages = [{"id": "owned-card"}]
    active = start + timedelta(seconds=5)
    rows: list[Message] = [
        {"jsonPayload": {"event": "discord.draining", "timestamp": active.isoformat()}},
        {"jsonPayload": {"event": "discord.draining", "timestamp": end.isoformat()}},
        {
            "jsonPayload": {
                "event": "turn.card_orphan_retirement_completed",
                "timestamp": active.isoformat(),
                "message_id": "owned-card",
            }
        },
        {
            "jsonPayload": {
                "event": "turn.card_orphan_retirement_completed",
                "timestamp": active.isoformat(),
                "message_id": "other-card",
            }
        },
    ]

    def logs(query: str) -> list[Message]:
        assert "owned-card" in query and "discord.draining" in query
        return rows

    monkeypatch.setattr(backend, "_read_logs", logs)
    evidence = backend.deployment_events(start, end, [turn])
    assert [row["affects_turn"] for row in evidence] == [True, False, True]
