"""Private Slack GitHub connect command presentation."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.slack import github_connect as github_command
from daimon.adapters.slack.runtime import SlackRuntime


class _Sessions:
    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield object()

    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_connect_returns_ephemeral_url_button(monkeypatch: pytest.MonkeyPatch) -> None:
    client = SimpleNamespace(
        users_info=AsyncMock(return_value={"user": {"team_id": "T1"}}),
        chat_postEphemeral=AsyncMock(),
    )
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
    monkeypatch.setattr(github_command, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(github_command, "resolve_is_admin", AsyncMock(return_value=True))
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
    audit = AsyncMock()
    monkeypatch.setattr(github_command, "append_event", audit)
    await github_command.handle_github_command(
        cast(SlackRuntime, runtime),
        {"team_id": "T1", "user_id": "U1", "channel_id": "C1", "text": "connect ResearchBot"},
    )
    kwargs = client.chat_postEphemeral.await_args.kwargs
    assert kwargs["channel"] == "C1" and kwargs["user"] == "U1"
    assert "https://" not in kwargs["text"]
    button = kwargs["blocks"][1]["elements"][0]
    assert button["text"]["text"] == "Connect GitHub"
    assert button["url"] == "https://mcp.test/oauth/github/connect/private-token"
    assert audit.await_args.kwargs["reason"] == "admin link minted"
    assert audit.await_args.kwargs["platform"] == "slack"
