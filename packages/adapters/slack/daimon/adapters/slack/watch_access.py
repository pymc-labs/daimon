"""A Slack watch reports only where its owner may still post: re-read from Slack before
every report. A proved loss pauses the watch; a Slack outage refuses that report alone."""

from __future__ import annotations

from typing import cast

from daimon.adapters.slack.routine_delivery import (  # pyright: ignore[reportPrivateUsage]
    _UNUSABLE_DESTINATION,  # pyright: ignore[reportPrivateUsage]
    _is_member,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.continuity.watch_turn import pause_for_lost_access
from daimon.core.routine_destinations import slack_may_deliver
from daimon.core.stores.domain import TaskContinuationRow
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def require_watch_post_access(
    sessions: async_sessionmaker[AsyncSession], row: TaskContinuationRow, client: AsyncWebClient
) -> None:
    """The routine rule (`slack_may_deliver`) for the watch owner in the reporting channel."""
    owner, channel_id = row.requester_external_user_id, row.parent_channel_id
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        channel = cast("dict[str, object]", info["channel"])
        user = cast("dict[str, object]", (await client.users_info(user=owner))["user"])  # pyright: ignore[reportUnknownMemberType]
        is_private = bool(channel.get("is_private"))
        is_guest = bool(user.get("is_restricted") or user.get("is_ultra_restricted"))
        allowed = not user.get("deleted") and slack_may_deliver(
            bot_is_member=bool(channel.get("is_member") or channel.get("is_im")),
            is_archived=bool(channel.get("is_archived")),
            is_im_or_mpim=bool(channel.get("is_im") or channel.get("is_mpim")),
            is_private=is_private,
            is_guest=is_guest,
            caller_is_member=(is_private or is_guest)
            and await _is_member(client, channel_id=channel_id, user_id=owner),
        )
    except SlackApiError as err:
        error = str(cast("dict[str, object]", err.response.data).get("error", ""))  # pyright: ignore[reportUnknownMemberType]
        if error not in _UNUSABLE_DESTINATION and error != "user_not_found":
            raise
        allowed = False
    if not allowed:
        raise await pause_for_lost_access(sessions, row)
