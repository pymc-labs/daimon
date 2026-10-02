"""Slack /here resolves the card and posts it only to the invoker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.here import (  # pyright: ignore[reportPrivateUsage]
    _plain_blocks,
    _visible_channel_ids,
    handle_here_command,
)


async def test_member_sees_public_bot_channels_and_shared_private_channels() -> None:
    client = MagicMock()
    client.users_conversations = AsyncMock(
        side_effect=[
            {
                "channels": [{"id": "C1", "is_private": False}],
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "channels": [{"id": "G1", "is_private": True}],
                "response_metadata": {"next_cursor": ""},
            },
            {
                "channels": [{"id": "G1", "is_private": True}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )
    client.users_info = AsyncMock(return_value={"user": {}})
    assert await _visible_channel_ids(client, "U1") == {"C1", "G1"}
    assert client.users_conversations.await_args_list[2].kwargs["types"] == "private_channel"


async def test_guest_sees_only_shared_channels() -> None:
    client = MagicMock()
    client.users_conversations = AsyncMock(
        side_effect=[
            {"channels": [{"id": "C1", "is_private": False}, {"id": "C2", "is_private": False}]},
            {"channels": [{"id": "C2", "is_private": False}]},
        ]
    )
    client.users_info = AsyncMock(return_value={"user": {"is_restricted": True}})
    assert await _visible_channel_ids(client, "U1") == {"C2"}


def test_card_blocks_keep_names_plain() -> None:
    blocks = _plain_blocks("**Here**\nWho answers: <!here>.")
    assert blocks[1]["text"]["type"] == "plain_text"
    assert "<!here>" in blocks[1]["text"]["text"]


async def test_here_posts_ephemeral_card() -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(return_value={"channel": {"is_private": False}})
    client.users_conversations = AsyncMock(return_value={"channels": []})
    client.users_info = AsyncMock(return_value={"user": {}})
    client.chat_postEphemeral = AsyncMock()
    runtime = MagicMock()
    runtime.settings.github.fallback_pat = None
    runtime.settings.github.app_id = None
    runtime.settings.github.app_private_key = None
    runtime.settings.mcp.public_url = None
    runtime.sessionmaker.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    runtime.sessionmaker.return_value.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("daimon.adapters.slack.here.resolve_web_client", new=AsyncMock(return_value=client)),
        patch("daimon.adapters.slack.here.resolve_is_admin", new=AsyncMock(return_value=False)),
        patch(
            "daimon.adapters.slack.here.find_platform_principal", new=AsyncMock(return_value=None)
        ),
        patch(
            "daimon.adapters.slack.here.load_here_card",
            new=AsyncMock(return_value=SimpleNamespace(text="fixed card")),
        ) as load,
    ):
        await handle_here_command(runtime, {"team_id": "T1", "channel_id": "C1", "user_id": "U1"})
    assert load.await_count == 1
    assert load.await_args.kwargs["bot_can_view"] is False
    client.chat_postEphemeral.assert_awaited_once_with(
        channel="C1",
        user="U1",
        text="Here status card",
        blocks=_plain_blocks("fixed card"),
        parse="none",
        link_names=False,
    )
