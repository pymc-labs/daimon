"""The shared Discord button never reveals its URL to another person."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from daimon.adapters.discord import github_connect_button as button_module
from daimon.core.github_credentials import build_multifernet, encrypt_token
from pydantic import SecretStr


class _Sessions:
    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_other_member_gets_only_ephemeral_refusal() -> None:
    button = button_module.GitHubConnectButton(requester_id="456", token_hash="a" * 64)
    interaction = AsyncMock()
    interaction.user.id = 999
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    interaction.response.send_message.assert_awaited_once_with(
        "Only <@456> can use this.", ephemeral=True
    )
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_requester_gets_ephemeral_link_and_click_sets_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = SecretStr(Fernet.generate_key().decode())
    encrypted = encrypt_token(build_multifernet((key.get_secret_value(),)), "private-link-token")
    bind = AsyncMock(return_value=(encrypted, "ResearchBot"))
    monkeypatch.setattr(button_module, "bind_discord_connect_click", bind)
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            crypto=SimpleNamespace(keys=[key]),
            mcp=SimpleNamespace(app_root_url="https://mcp.test"),
        ),
        sessionmaker=_Sessions(),
    )
    interaction = AsyncMock()
    interaction.user.id = 456
    interaction.guild_id = 123
    interaction.channel_id = 789
    interaction.application_id = 1234
    interaction.token = "interaction-token"
    interaction.client.runtime = runtime
    button = button_module.GitHubConnectButton(requester_id="456", token_hash="a" * 64)
    assert len(button.item.custom_id or "") <= 100
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert bind.await_args is not None
    assert bind.await_args.kwargs["requester_platform_user_id"] == "456"
    assert bind.await_args.kwargs["thread_id"] == "789"
    assert bind.await_args.kwargs["followup_expires_at"] > datetime.now(UTC)
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "https://" not in interaction.followup.send.await_args.args[0]
    assert interaction.followup.send.await_args.args[0] == "Connect GitHub for ResearchBot."
    assert kwargs["view"].children[0].url == (
        "https://mcp.test/oauth/github/connect/private-link-token"
    )
