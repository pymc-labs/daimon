"""Private GitHub connect command presentation."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.discord.commands import github as github_command


class _Sessions:
    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield object()

    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_connect_returns_ephemeral_url_button(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid.uuid4()
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            mcp=SimpleNamespace(app_root_url="https://mcp.test"),
            github_app=SimpleNamespace(
                app_id="1",
                app_slug="app",
                private_key="key",
                client_id="client",
                client_secret="secret",
            ),
        ),
        anthropic=object(),
        sessionmaker=_Sessions(),
    )
    interaction = AsyncMock()
    interaction.user.id = 123
    interaction.client.runtime = runtime
    monkeypatch.setattr(github_command, "is_guild_admin", lambda _interaction: True)  # pyright: ignore[reportUnknownArgumentType,reportUnknownLambdaType]
    monkeypatch.setattr(
        github_command, "resolve_tenant_for_interaction", AsyncMock(return_value=tenant_id)
    )
    monkeypatch.setattr(
        github_command,
        "list_agents_by_tenant",
        AsyncMock(return_value=[SimpleNamespace(name="ResearchBot", id="ma-agent")]),
    )
    monkeypatch.setattr(github_command, "pending_update_for_agent", AsyncMock(return_value=None))
    monkeypatch.setattr(github_command, "require_app_eligible_agent", AsyncMock())
    monkeypatch.setattr(
        github_command,
        "get_or_create_platform_principal",
        AsyncMock(return_value=SimpleNamespace(account_id=uuid.uuid4())),
    )
    monkeypatch.setattr(github_command, "set_role", AsyncMock())
    monkeypatch.setattr(github_command, "mint_invitation", AsyncMock(return_value="private-token"))
    cog = github_command.GitHubCog(AsyncMock())
    await github_command.GitHubCog.connect.callback.__wrapped__(  # type: ignore[attr-defined]
        cog, interaction, "ResearchBot"
    )
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "https://" not in interaction.followup.send.await_args.args[0]
    button = kwargs["view"].children[0]
    assert button.label == "Connect GitHub"
    assert button.url == "https://mcp.test/oauth/github/connect/private-token"
