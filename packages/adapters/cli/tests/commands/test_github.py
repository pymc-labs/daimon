"""GitHub operator command contract."""

import re
import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands import github as github_command
from daimon.adapters.cli.commands.github import _grant_session_action, github_app
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.session_snapshot import SessionSnapshot
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


@pytest.mark.asyncio
async def test_connect_link_agent_must_exist_in_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid.uuid4()
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id="ma-agent")
    listed = AsyncMock(return_value=[SimpleNamespace(id="ma-agent", name="ResearchBot")])
    monkeypatch.setattr(github_command, "list_agents_by_tenant", listed)
    client = cast(AsyncAnthropic, object())
    await github_command._require_tenant_agent(  # pyright: ignore[reportPrivateUsage]
        client, tenant_id=tenant_id, agent_id=agent_id, agent_name="ResearchBot"
    )
    with pytest.raises(ValueError, match="current agent in this workspace"):
        await github_command._require_tenant_agent(  # pyright: ignore[reportPrivateUsage]
            client, tenant_id=tenant_id, agent_id=uuid.uuid4(), agent_name="ResearchBot"
        )
    with pytest.raises(ValueError, match="current agent in this workspace"):
        await github_command._require_tenant_agent(  # pyright: ignore[reportPrivateUsage]
            client, tenant_id=tenant_id, agent_id=agent_id, agent_name="OtherBot"
        )
    listed.return_value = []
    with pytest.raises(ValueError, match="current agent in this workspace"):
        await github_command._require_tenant_agent(  # pyright: ignore[reportPrivateUsage]
            client, tenant_id=tenant_id, agent_id=agent_id, agent_name="ResearchBot"
        )


def test_grant_edit_rotates_only_when_app_repo_set_is_unchanged() -> None:
    snapshot = SessionSnapshot(
        ma_agent_id="agent",
        model_id="model",
        system_sha256=None,
        skills_sha256="skills",
        environment_id="env",
        github_mode="app",
        repo_urls=("https://github.com/acme/repo",),
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id="session-vault",
        tools_sha256="tools",
        mcp_servers_sha256="mcp",
        env_sha256=None,
        agent_version=1,
        agent_name="agent",
    )
    assert _grant_session_action("stage", snapshot, snapshot.repo_urls) == "rotate"
    assert _grant_session_action("remove", snapshot, snapshot.repo_urls) == "rotate"
    assert _grant_session_action("stage", snapshot, ()) == "close"
    assert _grant_session_action("remove", snapshot, ()) == "close"
    assert _grant_session_action("activate", snapshot, snapshot.repo_urls) == "close"
