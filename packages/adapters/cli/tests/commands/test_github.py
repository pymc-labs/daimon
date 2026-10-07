"""GitHub operator command contract."""

import uuid

from daimon.adapters.cli.commands.github import github_app
from typer.testing import CliRunner


def test_connect_link_requires_platform_requester_and_explains_authority() -> None:
    runner = CliRunner()
    help_result = runner.invoke(github_app, ["connect-link", "--help"])
    assert help_result.exit_code == 0
    assert "--requester" in help_result.output
    assert "on that admin's behalf" in help_result.output

    missing = runner.invoke(github_app, ["connect-link", "--tenant", str(uuid.uuid4())])
    assert missing.exit_code != 0
    assert "--requester" in missing.output
