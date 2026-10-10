import json
import subprocess
from pathlib import Path

import pytest

from qa.live.config import Config, Pricing
from qa.live.cost import Ledger
from qa.live.discord import DiscordBackend
from qa.live.evaluate import evaluate
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Pending


def test_cli_readback_positive_negative_and_all_fields() -> None:
    command = "daimon agents --guild 1435062989119295640 get qa-test"
    backend = FakeBackend()
    assertion = Assertion(
        kind="cli_check",
        cmd=command,
        expect="qa-original",
        expect_absent="deepwiki",
        expect_all_of='["qa-echo-skill", "claude-haiku-5-5"]',
    )
    assert evaluate(assertion, [], backend, FakeJudge()).status == "PASS"
    assertion.expect_absent = "qa-echo-skill"
    assert evaluate(assertion, [], backend, FakeJudge()).status == "FAIL"
    assertion.expect_absent = None
    assertion.expect_all_of = ["missing-skill"]
    assert evaluate(assertion, [], backend, FakeJudge()).status == "FAIL"


def test_cli_empty_readback_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(backend, "cli_read", lambda command: "")
    assert (
        evaluate(
            Assertion(kind="cli_check", cmd="read", expect="ok"), [], backend, FakeJudge()
        ).status
        == "PENDING"
    )


def test_cli_mutation_and_foreign_guild_never_invoke_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.model_validate_json(Path("qa/live/config.example.json").read_text())
    config.admin_hooks["cli_check"] = ["fake-hook"]
    config.staging.enabled = True
    backend = DiscordBackend(config, "staging")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid command invoked the hook")

    monkeypatch.setattr(subprocess, "run", forbidden)
    for command in [
        "daimon agents --guild 123 get qa-test",
        f"daimon agents --guild {config.staging.guild_id} archive qa-test",
        "daimon agents --guild 123 get qa-test; touch /tmp/bad",
        f"daimon agents --guild {config.staging.guild_id} get 'qa-test;touch /tmp/bad'",
        f"daimon agents --guild {config.staging.guild_id} get --flag",
        f"daimon agents --guild {config.staging.guild_id} get $(touch${{IFS}}/tmp/bad)",
        f"daimon agents --guild {config.staging.guild_id} get 'qa-test",
    ]:
        with pytest.raises(Pending):
            backend.cli_read(command)


def test_cli_hook_payload_redaction_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.model_validate_json(Path("qa/live/config.example.json").read_text())
    config.admin_hooks["cli_check"] = ["fake-hook", "--read-only"]
    config.staging.enabled = True
    backend = DiscordBackend(config, "staging")
    command = f"daimon agents --guild {config.staging.guild_id} get qa-test"
    response = subprocess.CompletedProcess(
        ["fake-hook"], 0, "qa-test api_key=private-test-value", ""
    )

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["fake-hook", "--read-only"]
        assert json.loads(str(kwargs["input"])) == {
            "guild_id": config.staging.guild_id,
            "argv": command.split(),
            "read_only": True,
        }
        assert kwargs == {
            "input": kwargs["input"],
            "capture_output": True,
            "text": True,
            "timeout": 60,
            "check": False,
        }
        return response

    monkeypatch.setattr(subprocess, "run", run)
    assert "qa-test" in backend.cli_read(command)
    assert "private-test-value" not in backend.cli_read(command)
    response.returncode = 1
    with pytest.raises(Pending, match="exited 1"):
        backend.cli_read(command)
    response.returncode = 0
    response.stdout = "é" * 65537
    with pytest.raises(Pending, match="bounded observation"):
        backend.cli_read(command)


@pytest.mark.parametrize(
    "error", [FileNotFoundError("missing hook"), subprocess.TimeoutExpired("hook", 60)]
)
def test_cli_hook_unavailable_is_pending(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    config = Config.model_validate_json(Path("qa/live/config.example.json").read_text())
    config.admin_hooks["cli_check"] = ["fake-hook"]
    config.staging.enabled = True
    backend = DiscordBackend(config, "staging")

    def run(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(Pending, match="unavailable"):
        backend.cli_read(f"daimon agents --guild {config.staging.guild_id} get qa-test")


@pytest.mark.parametrize("patterns", ["not JSON", '["valid", 1]', '["["]'])
def test_cli_bad_resolved_expectations_do_not_stop_other_checks(
    patterns: str, scenario: Scenario, ledger: Ledger, pricing: Pricing
) -> None:
    backend = FakeBackend()
    unavailable = Assertion(kind="cli_check", cmd="read", expect_all_of=patterns)
    valid = Assertion(kind="cli_check", cmd="read", expect="qa-original")
    scenario.steps = []
    scenario.est_turns = 0
    scenario.assertions = [unavailable, valid]
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert [(check.kind, check.status) for check in result.checks] == [
        ("cli_check", "PENDING"),
        ("cli_check", "PASS"),
    ]
    assert not result.errors
    assert ledger.charged({result.run_id}) == 0
