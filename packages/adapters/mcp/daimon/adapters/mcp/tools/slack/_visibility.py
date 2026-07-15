"""Per-user channel visibility policy for the Slack channel tools.

Universe is capped by the bot token (only channels the bot is in are
reachable). Within it: public channels are visible to full workspace members
without a membership check; guests (``is_restricted``/``is_ultra_restricted``)
and all private channels require the caller to be a channel member. im/mpim
are always rejected. Denials use one message ("missing channel access") for
both not-found and forbidden so private channel existence never leaks.
"""

from __future__ import annotations

from typing import Any, cast

from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

MISSING_ACCESS = "missing channel access"
MISSING_SCOPE_MSG = (
    "this workspace's daimon install is missing the channel-read scopes — "
    "a workspace admin must re-install daimon from the install link to grant them"
)
# A user token's scopes are frozen at connect time — re-installing the app
# cannot refresh them, only a fresh per-user grant can.
MISSING_SCOPE_USER_MSG = (
    "your connected Slack authorization is missing scopes daimon now needs — "
    "disconnect and reconnect via /privacy to grant them"
)
# Copy is worded generically ("your Slack authorization") rather than
# user-token-specific: this same code fires on a revoked/expired bot token too,
# and the fix (disconnect + reconnect via /privacy) is the right instruction
# for a user token, while a bot-token auth failure needs a workspace
# reinstall — but we can't tell which token issued the call from the error
# alone, so this message stays truthful-enough for both rather than picking one.
INVALID_AUTH_MSG = (
    "your Slack authorization is no longer valid — disconnect and reconnect via /privacy"
)


def is_channel_visible(
    *, is_im_or_mpim: bool, is_private: bool, is_guest: bool, is_member: bool
) -> bool:
    """Pure visibility decision — mirrors Slack's own semantics."""
    if is_im_or_mpim:
        return False
    if is_private or is_guest:
        return is_member
    return True


def map_slack_api_error(err: SlackApiError, *, as_user: bool = False) -> ToolError | None:
    """Map known Slack API errors to caller-facing ToolErrors; None → re-raise.

    ``as_user`` marks calls issued with the caller's own xoxp token, where a
    missing_scope needs a reconnect (fresh grant), not a workspace re-install.
    """
    error_code = str(err.response.get("error", ""))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
    if error_code == "missing_scope":
        return ToolError(MISSING_SCOPE_USER_MSG if as_user else MISSING_SCOPE_MSG)
    if error_code in ("channel_not_found", "not_in_channel"):
        return ToolError(MISSING_ACCESS)
    if error_code in ("token_revoked", "token_expired", "invalid_auth"):
        return ToolError(INVALID_AUTH_MSG)
    return None


async def _is_guest(client: AsyncWebClient, *, user_id: str) -> bool:
    resp = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
    user = cast(dict[str, Any], resp["user"])
    return bool(user.get("is_restricted") or user.get("is_ultra_restricted"))


async def _is_user_in_channel(client: AsyncWebClient, *, channel_id: str, user_id: str) -> bool:
    cursor: str | None = None
    while True:
        resp = await client.conversations_members(  # pyright: ignore[reportUnknownMemberType]  # slack_sdk **kwargs: Unknown
            channel=channel_id, limit=200, cursor=cursor
        )
        if user_id in cast(list[str], resp["members"]):
            return True
        metadata = cast(dict[str, Any], resp.get("response_metadata") or {})
        cursor = str(metadata.get("next_cursor") or "") or None
        if cursor is None:
            return False


async def check_channel_access(
    client: AsyncWebClient, *, channel: dict[str, Any], user_id: str
) -> None:
    """Raise ToolError(MISSING_ACCESS) unless ``user_id`` may view ``channel``.

    ``channel`` is a ``conversations.info`` channel object. Public channels
    skip the membership scan for full members (one users.info call decides).
    """
    is_im_or_mpim = bool(channel.get("is_im") or channel.get("is_mpim"))
    if is_im_or_mpim:
        raise ToolError(MISSING_ACCESS)
    is_private = bool(channel.get("is_private"))
    is_guest = await _is_guest(client, user_id=user_id)
    needs_membership = is_private or is_guest
    is_member = (
        await _is_user_in_channel(client, channel_id=str(channel["id"]), user_id=user_id)
        if needs_membership
        else False
    )
    if not is_channel_visible(
        is_im_or_mpim=is_im_or_mpim,
        is_private=is_private,
        is_guest=is_guest,
        is_member=is_member,
    ):
        raise ToolError(MISSING_ACCESS)
