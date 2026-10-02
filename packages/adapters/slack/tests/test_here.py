"""Slack /here resolves the card and posts it only to the invoker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.here import (  # pyright: ignore[reportPrivateUsage]
    _plain_blocks,
    _shared_channel_ids,
    handle_here_command,
)


async def test_shared_channels_follow_all_pages() -> None:
    client = MagicMock()
    client.users_conversations = AsyncMock(
        side_effect=[
            {"channels": [{"id": "C1"}], "response_metadata": {"next_cursor": "next"}},
            {"channels": [{"id": "C2"}], "response_metadata": {"next_cursor": ""}},
        ]
    )
    assert await _shared_channel_ids(client, "U1") == {"C1", "C2"}


def test_card_blocks_keep_names_plain() -> None:
    blocks = _plain_blocks("**Here**\nWho answers: <!here>.")
    assert blocks[1]["text"]["type"] == "plain_text"
    assert "<!here>" in blocks[1]["text"]["text"]


async def test_here_posts_ephemeral_card() -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(return_value={"channel": {"is_private": False}})
    client.users_conversations = AsyncMock(return_value={"channels": []})
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
    client.chat_postEphemeral.assert_awaited_once_with(
        channel="C1",
        user="U1",
        text="Here status card",
        blocks=_plain_blocks("fixed card"),
        parse="none",
        link_names=False,
    )
