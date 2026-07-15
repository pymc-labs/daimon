from __future__ import annotations

from daimon.adapters.cli.main import app
from typer.testing import CliRunner


def test_help_agents_prints_embedded_markdown() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["help", "agents"])
    assert result.exit_code == 0
    assert "# `daimon` — agentic CLI reference" in result.stdout
    assert "## Exit codes" in result.stdout
