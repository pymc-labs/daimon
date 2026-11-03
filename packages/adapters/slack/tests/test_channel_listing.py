"""Slack lists every public channel and the private ones the bot is in, page by page."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from daimon.adapters.slack.channel_listing import info_lookup, list_workspace
from daimon.core.platform_names import ChannelStatus
from slack_sdk.errors import SlackApiError
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


async def test_info_lookup_tells_a_name_a_direct_message_and_a_gone_channel() -> None:
    def answer(channel: dict[str, Any]) -> MagicMock:
        resp = MagicMock()
        resp.data = {"channel": channel}
        return resp

    def failure(error: str) -> SlackApiError:
        return SlackApiError("failed", MagicMock(data={"error": error}))

    client = MagicMock()
    client.conversations_info = AsyncMock(
        side_effect=[
            answer({"name": "pricing"}),
            answer({"is_im": True}),
            failure("channel_not_found"),
            failure("ratelimited"),
        ]
    )
    lookup = info_lookup(cast(AsyncWebClient, client))
    found = [await lookup("T1", cid) for cid in ("C1", "D1", "C2", "C3")]
    assert found == ["pricing", ChannelStatus.DIRECT, ChannelStatus.GONE, None]
