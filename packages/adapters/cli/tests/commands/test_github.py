"""GitHub operator command contract."""

import re
import uuid

from daimon.adapters.cli.commands.github import github_app
from typer.testing import CliRunner


def _plain(output: str) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def test_connect_link_requires_platform_requester_and_explains_authority() -> None:
    runner = CliRunner()
    help_result = runner.invoke(github_app, ["connect-link", "--help"])
    assert help_result.exit_code == 0
    assert "--requester" in _plain(help_result.output)
    assert "on that admin's behalf" in _plain(help_result.output)

    missing = runner.invoke(github_app, ["connect-link", "--tenant", str(uuid.uuid4())])
    assert missing.exit_code != 0
    assert "--requester" in _plain(missing.output)
