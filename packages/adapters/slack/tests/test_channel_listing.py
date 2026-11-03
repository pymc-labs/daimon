"""Slack lists every public channel and the private ones the bot is in, page by page."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from daimon.adapters.slack.channel_listing import list_workspace
from slack_sdk.web.async_client import AsyncWebClient


def _page(channels: list[dict[str, Any]], cursor: str = "") -> MagicMock:
    resp = MagicMock()
    resp.data = {"channels": channels, "response_metadata": {"next_cursor": cursor}}
    return resp


async def test_pages_are_followed_and_unseen_private_channels_left_out() -> None:
    client = MagicMock()
    client.conversations_list = AsyncMock(
        side_effect=[
            _page([{"id": "C1", "name": "general"}], cursor="next"),
            _page(
                [
                    {"id": "C2", "name": "old", "is_archived": True},
                    {"id": "G1", "name": "team", "is_private": True, "is_member": True},
                    {"id": "G2", "name": "hidden", "is_private": True, "is_member": False},
                ]
            ),
        ]
    )
    found = await list_workspace(cast(AsyncWebClient, client), page_gap_s=0)
    assert {k: (v.name, v.kind, v.archived) for k, v in found.items()} == {
        "C1": ("general", "public", False),
        "C2": ("old", "public", True),
        "G1": ("team", "private", False),
    }
    assert client.conversations_list.await_args_list[1].kwargs["cursor"] == "next"
