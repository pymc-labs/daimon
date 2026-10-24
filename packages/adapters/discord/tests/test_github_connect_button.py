"""The shared Discord button reveals its URL only privately, to whoever clicks it."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
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
async def test_member_is_refused_before_a_link_is_minted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = SecretStr(Fernet.generate_key().decode())
    encrypted = encrypt_token(build_multifernet((key.get_secret_value(),)), "private-link-token")
    bind = AsyncMock(return_value=(encrypted, "ResearchBot"))
    refusal = AsyncMock(return_value=None)
    monkeypatch.setattr(button_module, "bind_discord_connect_click", bind)
    monkeypatch.setattr(button_module, "_manager_refusal", refusal)
    monkeypatch.setattr(button_module, "is_member_guild_admin", lambda *_args, **_kwargs: False)
    requester = SimpleNamespace(id=456)
    interaction = AsyncMock()
    interaction.user.id = 999
    interaction.response.is_done = lambda: False
    interaction.guild_id = 123
    interaction.channel_id = 789
    interaction.client.runtime = SimpleNamespace(
        settings=SimpleNamespace(
            crypto=SimpleNamespace(keys=[key]),
            mcp=SimpleNamespace(app_root_url="https://mcp.test"),
        ),
        sessionmaker=_Sessions(),
    )
    interaction.guild = SimpleNamespace(owner_id=1, fetch_member=AsyncMock(return_value=requester))
    button = button_module.GitHubConnectButton(requester_id="456", intent_id=uuid.UUID(int=1))
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    interaction.response.send_message.assert_awaited_once()
    assert "platform admin" in interaction.response.send_message.await_args.args[0]
    interaction.guild.fetch_member.assert_not_awaited()
    refusal.assert_not_awaited()
    bind.assert_not_awaited()


@pytest.mark.asyncio
async def test_requester_gets_ephemeral_link_and_click_sets_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = SecretStr(Fernet.generate_key().decode())
    encrypted = encrypt_token(build_multifernet((key.get_secret_value(),)), "private-link-token")
    bind = AsyncMock(return_value=(encrypted, "ResearchBot"))
    monkeypatch.setattr(button_module, "bind_discord_connect_click", bind)
    monkeypatch.setattr(button_module, "is_member_guild_admin", lambda *_args, **_kwargs: True)
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            crypto=SimpleNamespace(keys=[key]),
            mcp=SimpleNamespace(app_root_url="https://mcp.test"),
        ),
        sessionmaker=_Sessions(),
    )
    interaction = AsyncMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 456
    interaction.user.guild_permissions = discord.Permissions(administrator=True)
    interaction.guild_id = 123
    interaction.channel_id = 789
    interaction.application_id = 1234
    interaction.token = "interaction-token"
    interaction.client.runtime = runtime

    async def fetch_member(_user_id: int) -> object:
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        return object()

    interaction.guild = SimpleNamespace(
        owner_id=456, fetch_member=AsyncMock(side_effect=fetch_member)
    )
    button = button_module.GitHubConnectButton(requester_id="456", intent_id=uuid.UUID(int=1))
    assert button.item.emoji.name == "🔗"
    assert len(button.item.custom_id or "") <= 100
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert bind.await_args is not None
    assert bind.await_args.kwargs["requester_platform_user_id"] == "456"
    assert bind.await_args.kwargs["thread_id"] == "789"
    assert bind.await_args.kwargs["followup_expires_at"] > datetime.now(UTC)
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert interaction.followup.send.await_args.args == ()
    assert (
        kwargs["embed"].description
        == "Nothing is connected yet.\n\nTap the button and tick the repos ResearchBot can use."
    )
    assert kwargs["embed"].to_dict()["author"]["name"] == "Daimon"
    assert kwargs["view"].children[0].url == (
        "https://mcp.test/oauth/github/connect/private-link-token"
    )
    assert kwargs["view"].children[0].emoji.name == "🔗"
