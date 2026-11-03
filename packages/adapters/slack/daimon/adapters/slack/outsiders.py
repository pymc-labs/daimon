"""Fresh current-channel sharing evidence; Slack guest accounts are not checked."""

from __future__ import annotations

from typing import Any, cast

from aiohttp import ClientError
from daimon.core.outsiders import OUTSIDER_CHECK_SKIPPED, OutsiderVisibility
from slack_sdk.errors import SlackApiError, SlackClientError
from slack_sdk.web.async_client import AsyncWebClient

SLACK_GUESTS_UNCHECKED = (
    "Guest accounts aren't checked on Slack; if this channel has guests, make it confidential."
)
# Slack's conversation/channel objects describe external, Enterprise and pending
# sharing. Pending sharing is deliberately treated as shared, as Slack recommends.
# https://docs.slack.dev/reference/objects/conversation-object/
# https://docs.slack.dev/reference/objects/channel-object/
SHARED_FLAGS = (
    "is_shared",
    "is_ext_shared",
    "is_org_shared",
    "is_ext_ws_shared",
    "is_pending_ext_shared",
    "is_global_shared",
    "is_org_default",
    "is_org_mandatory",
)
SHARED_WORKSPACE_LISTS = (
    "shared_team_ids",
    "internal_team_ids",
    "connected_team_ids",
    "pending_shared",
    "pending_connected_team_ids",
)


class SlackVisibility(OutsiderVisibility):
    bot_can_view: bool | None = None


def _sharing_evidence(channel: dict[str, Any], *, team_id: str) -> bool | None:
    incomplete = not any(isinstance(channel.get(key), bool) for key in SHARED_FLAGS)
    shared = False
    for key in SHARED_FLAGS:
        if key in channel:
            value = channel[key]
            if not isinstance(value, bool):
                incomplete = True
            elif value:
                shared = True
    for key in SHARED_WORKSPACE_LISTS:
        if key not in channel:
            continue
        values = channel[key]
        if not isinstance(values, list):
            incomplete = True
            continue
        for value in cast(list[Any], values):
            if not isinstance(value, str) or not value:
                incomplete = True
            elif key.startswith("pending_") or value != team_id:
                shared = True
    return True if shared else None if incomplete else False


async def channel_outsiders(
    client: AsyncWebClient, *, team_id: str, channel_id: str
) -> SlackVisibility:
    """Use one response in this request; never cache or infer guest membership."""
    bot_view: bool | None = None
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        raw_channel = info.get("channel")
        if isinstance(raw_channel, dict):
            channel = cast(dict[str, Any], raw_channel)
            bot_view = bool(channel.get("is_member"))
            shared = _sharing_evidence(channel, team_id=team_id)
            if shared is not None:
                # An unshared channel can still contain guests. Never represent
                # the unperformed guest check as proof that no outsiders exist.
                return SlackVisibility(
                    outsiders_can_view=True if shared else None,
                    note=SLACK_GUESTS_UNCHECKED,
                    bot_can_view=bot_view,
                )
    except SlackApiError as exc:
        data = cast(dict[str, Any], exc.response.data)  # pyright: ignore[reportUnknownMemberType]
        if data.get("error") in {"channel_not_found", "not_in_channel"}:
            bot_view = False
    except (SlackClientError, ClientError, TimeoutError):
        pass
    return SlackVisibility(
        note=f"{OUTSIDER_CHECK_SKIPPED}\n{SLACK_GUESTS_UNCHECKED}", bot_can_view=bot_view
    )
