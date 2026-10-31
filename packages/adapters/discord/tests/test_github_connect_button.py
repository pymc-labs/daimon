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

    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


_AGENT_NAME = "ResearchBot"
_MA_AGENT = "ma-agent"


def _press(
    monkeypatch: pytest.MonkeyPatch,
    *,
    clicker_id: int,
    clicker_is_admin: bool = False,
    clicker_owns: bool = False,
    requester_refusal: str | None = None,
) -> tuple[AsyncMock, AsyncMock, AsyncMock, AsyncMock, MagicMock]:
    """A card for requester 456 pressed by `clicker_id`; returns bind, audit and checks."""
    key = SecretStr(Fernet.generate_key().decode())
    encrypted = encrypt_token(build_multifernet((key.get_secret_value(),)), "private-link-token")
    bind = AsyncMock(return_value=(encrypted, _AGENT_NAME))
    audit = AsyncMock()
    owner_check = AsyncMock(return_value=clicker_owns)
    refusal = AsyncMock(return_value=requester_refusal)
    monkeypatch.setattr(button_module, "bind_discord_connect_click", bind)
    monkeypatch.setattr(button_module, "append_event", audit)
    monkeypatch.setattr(button_module, "can_manage_agent_github", owner_check)
    monkeypatch.setattr(button_module, "_manager_refusal", refusal)
    monkeypatch.setattr(
        button_module,
        "connect_intent_agent",
        AsyncMock(return_value=(_AGENT_NAME, _MA_AGENT)),
    )
    monkeypatch.setattr(
        button_module,
        "find_agent_by_derived_uuid",
        AsyncMock(return_value=SimpleNamespace(name=_AGENT_NAME, metadata={})),
    )
    # Only the clicker may be a live admin; the requester is an ordinary member.
    monkeypatch.setattr(
        button_module,
        "is_member_guild_admin",
        lambda member, **_kwargs: clicker_is_admin and member.id == clicker_id,
    )
    interaction = AsyncMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = clicker_id
    interaction.response.is_done = lambda: False
    interaction.guild_id = 123
    interaction.channel_id = 789
    interaction.application_id = 1234
    interaction.token = "interaction-token"
    interaction.client.runtime = SimpleNamespace(
        settings=SimpleNamespace(
            crypto=SimpleNamespace(keys=[key]),
            mcp=SimpleNamespace(app_root_url="https://mcp.test"),
        ),
        sessionmaker=_Sessions(),
        anthropic=object(),
        deployment_default=object(),
    )
    interaction.guild = SimpleNamespace(
        owner_id=1, fetch_member=AsyncMock(return_value=SimpleNamespace(id=456))
    )
    return bind, audit, owner_check, refusal, interaction


@pytest.mark.asyncio
async def test_another_member_is_told_who_to_ask_and_gets_no_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card is the requester's: a bystander in the thread is not shown their link."""
    bind, audit, _, _, interaction = _press(monkeypatch, clicker_id=999)
    button = button_module.GitHubConnectButton(requester_id="456", intent_id=uuid.UUID(int=1))
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    bind.assert_not_awaited()
    audit.assert_not_awaited()
    interaction.guild.fetch_member.assert_not_awaited()
    assert interaction.followup.send.await_args.args == ("Ask <@456> or an owner of ResearchBot.",)
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True and "view" not in kwargs and "embed" not in kwargs


@pytest.mark.asyncio
async def test_an_owner_of_the_agent_is_shown_the_link_and_the_click_is_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind, audit, owner_check, refusal, interaction = _press(
        monkeypatch, clicker_id=999, clicker_owns=True
    )
    button = button_module.GitHubConnectButton(requester_id="456", intent_id=uuid.UUID(int=1))
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    # The owner is the clicker; the invitation and its manager check stay the requester's.
    assert owner_check.await_args.kwargs["caller"].platform_user_id == "999"
    assert refusal.await_args.args[1].id == 456
    assert bind.await_args.kwargs["requester_platform_user_id"] == "456"
    assert interaction.followup.send.await_args.kwargs["view"].children[0].url == (
        "https://mcp.test/oauth/github/connect/private-link-token"
    )
    event = audit.await_args.kwargs
    assert (event["platform_user_id"], event["reason"]) == (
        "999",
        "connect link shown to another person",
    )
    assert event["agent_id"] is not None


@pytest.mark.asyncio
async def test_a_live_admin_who_is_not_the_requester_is_shown_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bind, _, owner_check, _, interaction = _press(
        monkeypatch, clicker_id=999, clicker_is_admin=True
    )
    button = button_module.GitHubConnectButton(requester_id="456", intent_id=uuid.UUID(int=1))
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    bind.assert_awaited_once()
    owner_check.assert_not_awaited()
    assert interaction.followup.send.await_args.kwargs["view"].children[0].url


@pytest.mark.asyncio
async def test_an_owner_clicking_after_the_requester_lost_the_agent_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invitation is still the requester's, who must still manage the agent."""
    bind, audit, _, _, interaction = _press(
        monkeypatch,
        clicker_id=999,
        clicker_owns=True,
        requester_refusal="Ask whoever manages ResearchBot to add repos.",
    )
    button = button_module.GitHubConnectButton(requester_id="456", intent_id=uuid.UUID(int=1))
    await button._reveal(interaction)  # pyright: ignore[reportPrivateUsage]
    bind.assert_not_awaited()
    audit.assert_not_awaited()
    assert interaction.followup.send.await_args.args == (
        "Ask whoever manages ResearchBot to add repos.",
    )


@pytest.mark.asyncio
async def test_requester_gets_ephemeral_link_and_click_sets_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = SecretStr(Fernet.generate_key().decode())
    encrypted = encrypt_token(build_multifernet((key.get_secret_value(),)), "private-link-token")
    bind = AsyncMock(return_value=(encrypted, "ResearchBot"))
    monkeypatch.setattr(button_module, "bind_discord_connect_click", bind)
    monkeypatch.setattr(button_module, "is_member_guild_admin", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        button_module, "connect_intent_agent", AsyncMock(return_value=("ResearchBot", "ma-agent"))
    )
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
