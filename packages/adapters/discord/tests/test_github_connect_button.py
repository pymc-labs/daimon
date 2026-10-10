"""The shared Discord button never reveals its URL to another person."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    button = button_module.GitHubConnectButton(requester_id="456", intent_id=uuid.UUID(int=1))
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
    monkeypatch.setattr(button_module, "is_member_guild_admin", lambda *_args, **_kwargs: True)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "manages", "expected"),
    [
        (("helper", "ag_helper"), True, None),
        (("helper", "ag_helper"), False, "Ask whoever manages helper to add repos."),
        (("helper", None), True, "Ask whoever manages helper to add repos."),
        (None, True, "This connection is no longer available."),
    ],
)
async def test_non_admin_click_needs_to_manage_the_agent(
    monkeypatch: pytest.MonkeyPatch,
    target: tuple[str, str | None] | None,
    manages: bool,
    expected: str | None,
) -> None:
    @asynccontextmanager
    async def session():  # type: ignore[no-untyped-def]
        yield MagicMock()

    bot = MagicMock()
    bot.runtime.sessionmaker = session
    check = AsyncMock(return_value=manages)
    monkeypatch.setattr(button_module, "connect_intent_agent", AsyncMock(return_value=target))
    monkeypatch.setattr(
        button_module,
        "find_agent_by_derived_uuid",
        AsyncMock(return_value=SimpleNamespace(name="helper", metadata={})),
    )
    monkeypatch.setattr(button_module, "can_manage_agent_github", check)
    monkeypatch.setattr(button_module, "channel_admin_caller", lambda _member: "caller")
    refusal = await button_module._manager_refusal(  # pyright: ignore[reportPrivateUsage]
        bot, MagicMock(), uuid.uuid4(), uuid.uuid4()
    )
    assert refusal == expected
    if target is not None and target[1] is not None:
        assert check.await_args.kwargs["is_daimon_managed"] is False
        assert check.await_args.kwargs["caller"] == "caller"
