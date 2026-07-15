import importlib.metadata

import pytest
from daimon.adapters.cli.main import app
from typer.testing import CliRunner


def test_root_app_help_lists_all_subapps() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("agents", "environments", "config", "defaults"):
        assert name in result.stdout


def test_version_runs_without_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAIMON_DATABASE__URL", raising=False)
    monkeypatch.delenv("DAIMON_DATABASE__TEST_URL", raising=False)
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0, result.stdout + result.stderr
    expected_version = importlib.metadata.version("daimon-adapter-cli")
    assert f"daimon {expected_version}" in result.stdout, (
        "version command should echo the installed daimon-adapter-cli version"
    )


def test_help_agents_runs_without_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAIMON_DATABASE__URL", raising=False)
    monkeypatch.delenv("DAIMON_DATABASE__TEST_URL", raising=False)
    result = CliRunner().invoke(app, ["help", "agents"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.strip() != ""


def test_run_help_runs_without_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAIMON_DATABASE__URL", raising=False)
    monkeypatch.delenv("DAIMON_DATABASE__TEST_URL", raising=False)
    result = CliRunner().invoke(app, ["run", "--help"])
    assert result.exit_code == 0, result.stdout + result.stderr


def test_agents_list_still_fails_without_database_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAIMON_DATABASE__URL", raising=False)
    monkeypatch.delenv("DAIMON_DATABASE__TEST_URL", raising=False)
    monkeypatch.delenv("DAIMON_ANTHROPIC__API_KEY", raising=False)
    from daimon.core.config import load_settings as _real_load

    monkeypatch.setattr(
        "daimon.adapters.cli.commands.agents.load_settings",
        lambda: _real_load(_env_file=None),
    )
    result = CliRunner().invoke(app, ["agents", "list"])
    assert result.exit_code != 0, f"expected failure but got exit_code=0: {result.stdout}"
