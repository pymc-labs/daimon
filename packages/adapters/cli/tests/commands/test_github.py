"""GitHub operator command contract."""

import re
import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands import github as github_command
from daimon.adapters.cli.commands.github import (
    _grant_session_action,
    _is_legacy_repo_credential_url,
    github_app,
)
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
    assert _grant_session_action("add", snapshot, snapshot.repo_urls) == "rotate"
    assert _grant_session_action("remove", snapshot, snapshot.repo_urls) == "rotate"
    assert _grant_session_action("add", snapshot, ()) == "close"
    assert _grant_session_action("remove", snapshot, ()) == "close"
    assert _grant_session_action("activate", snapshot, snapshot.repo_urls) == "close"


def test_legacy_vault_cleanup_accepts_github_with_or_without_trailing_slash() -> None:
    assert _is_legacy_repo_credential_url("https://github.com")
    assert _is_legacy_repo_credential_url("https://github.com/")
    assert not _is_legacy_repo_credential_url("https://api.githubcopilot.com/mcp")
    for other in (
        "https://github.com//",
        "https://github.com/owner/repo",
        "https://GitHub.com",
        "http://github.com",
        "https://github.com:443",
        "https://github.com.example",
        None,
    ):
        assert not _is_legacy_repo_credential_url(other)


@pytest.mark.parametrize(
    ("url", "deleted"),
    [("https://github.com/", True), ("https://api.githubcopilot.com/mcp", False)],
)
def test_legacy_vault_cleanup_deletes_only_recorded_repo_credentials(
    monkeypatch: pytest.MonkeyPatch, url: str, deleted: bool
) -> None:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    updates: list[dict[str, object]] = []

    class _Session:
        async def execute(self, _statement: object, params: dict[str, object] | None = None):
            if params is not None:
                updates.append(params)
            return SimpleNamespace(all=lambda: [(tenant_id, agent_id, "cred_legacy")])

        async def __aenter__(self) -> "_Session":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    class _Factory:
        def __call__(self) -> _Session:
            return _Session()

        def begin(self) -> _Session:
            return _Session()

    class _Anthropic:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> "_Anthropic":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    async def vaults(*_: object, **__: object):
        yield SimpleNamespace(id="vault_1", display_name=f"daimon-mcp:account:{agent_id}")

    async def credentials(*_: object, **__: object):
        yield SimpleNamespace(id="cred_legacy", auth=SimpleNamespace(mcp_server_url=url))

    delete = AsyncMock()
    settings = SimpleNamespace(
        database=SimpleNamespace(url="postgresql+asyncpg://unused/db"),
        anthropic=SimpleNamespace(
            api_key=SimpleNamespace(get_secret_value=lambda: "unused"), base_url="https://unused"
        ),
    )
    monkeypatch.setattr(github_command, "load_settings", lambda: settings)

    def engine(_url: str) -> SimpleNamespace:
        return SimpleNamespace(dispose=AsyncMock())

    def factory(_engine: object) -> _Factory:
        return _Factory()

    monkeypatch.setattr(github_command, "build_engine", engine)
    monkeypatch.setattr(github_command, "build_session_factory", factory)
    monkeypatch.setattr(github_command, "AsyncAnthropic", _Anthropic)
    monkeypatch.setattr(github_command, "list_vaults", vaults)
    monkeypatch.setattr(github_command, "list_credentials", credentials)
    monkeypatch.setattr(github_command, "delete_credential", delete)

    result = CliRunner().invoke(github_app, ["cleanup-legacy-vault", "--apply"])

    if deleted:
        assert result.exit_code == 0, result.output
        delete.assert_awaited_once()
        assert updates == [{"tenant": tenant_id, "agent": agent_id, "credential": "cred_legacy"}]
    else:
        assert result.exit_code != 0
        delete.assert_not_awaited()
        assert updates == []
