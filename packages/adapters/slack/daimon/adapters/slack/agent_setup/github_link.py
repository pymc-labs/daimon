"""Private Slack browser links for connecting GitHub repos."""

from __future__ import annotations

from typing import Any, cast

from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import CONNECT_COPY
from slack_sdk.web.async_client import AsyncWebClient

ACTION_COPY = "github_link__copy"


async def send_link(client: AsyncWebClient, *, channel_id: str, user_id: str, url: str) -> None:
    text = f"{CONNECT_COPY}\nLink works once · expires in 7 days"
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id,
        user=user_id,
        text=text,
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "github_link__open",
                        "text": {"type": "plain_text", "text": "Open GitHub ↗"},
                        "url": url,
                    },
                    {
                        "type": "button",
                        "action_id": ACTION_COPY,
                        "text": {"type": "plain_text", "text": "Copy link"},
                        "value": url,
                    },
                ],
            },
        ],
    )


async def handle_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    actions = cast("list[dict[str, Any]]", payload.get("actions") or [])
    if not actions or actions[0].get("action_id") != ACTION_COPY:
        return
    team = cast("dict[str, Any]", payload.get("team") or {})
    channel = cast("dict[str, Any]", payload.get("channel") or {})
    user = cast("dict[str, Any]", payload.get("user") or {})
    client = await resolve_web_client(runtime, team_id=str(team.get("id") or ""))
    if client is None:
        return
    url = str(actions[0].get("value") or "")
    if not url.startswith("https://"):
        return
    await post_ephemeral(
        client,
        channel_id=str(channel.get("id") or user.get("id") or ""),
        user_id=str(user.get("id") or ""),
        text=url,
    )
