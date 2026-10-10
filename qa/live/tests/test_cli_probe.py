import subprocess
from pathlib import Path

import pytest

from qa.live.config import Config
from qa.live.discord import DiscordBackend
from qa.live.evaluate import evaluate
from qa.live.schema import Assertion
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
    ]:
        with pytest.raises(Pending):
            backend.cli_read(command)
