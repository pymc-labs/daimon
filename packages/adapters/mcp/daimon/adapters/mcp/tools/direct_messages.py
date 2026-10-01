"""Tenant-scoped direct delivery through the shared channel-tool surface."""

from __future__ import annotations

import re
from typing import cast

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_dm_recipient_allowed
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.core.config import DirectMessagePolicy
from fastmcp.exceptions import ToolError
from pydantic import BaseModel
from slack_sdk.errors import SlackApiError


class DirectMessageResult(BaseModel):
    platform: str
    recipient_id: str
    channel_id: str
    message_ids: list[str]


async def send_direct_message_impl(
    runtime: McpRuntime, auth: AuthIdentity, *, recipient_id: str, content: str
) -> DirectMessageResult:
    """Validate policy before opening a DM; verify both people in the live tenant."""
    if auth.platform not in {"discord", "slack"}:
        raise ToolError("direct messages are not supported on this platform")
    if not content.strip() or len(content) > 19000:
        raise ToolError("content must contain between 1 and 19000 characters")
    pattern = r"[0-9]+" if auth.platform == "discord" else r"[UW][A-Z0-9]+"
    if re.fullmatch(pattern, recipient_id) is None:
        raise ToolError("recipient_id must be one platform user ID, not a mention or channel")
    policy = runtime.settings.direct_message_policies.get(auth.tenant_id, DirectMessagePolicy())
    if not policy.allows(recipient_id):
        raise ToolError("recipient is denied by this tenant's direct-message policy")
    await require_dm_recipient_allowed(runtime, auth, recipient_id=recipient_id)
    chunks = [content[i : i + 1900] for i in range(0, len(content), 1900)]
    ids: list[str] = []
    if auth.platform == "discord":
        caller = _require_discord_identity(auth)
        guild_id = _require_guild_id(auth)
        try:
            async with rest_client(_require_bot_token(runtime)) as client:
                guild = await client.fetch_guild(int(guild_id))
                await guild.fetch_member(int(caller))
                recipient = await guild.fetch_member(int(recipient_id))
                if recipient.bot:
                    raise ToolError("recipient must be a human tenant member")
                dm = await recipient.create_dm()
                for chunk in chunks:
                    message = await dm.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                    ids.append(str(message.id))
                return DirectMessageResult(
                    platform="discord",
                    recipient_id=recipient_id,
                    channel_id=str(dm.id),
                    message_ids=ids,
                )
        except discord.HTTPException as exc:
            raise ToolError(
                f"Discord DM failed after {len(ids)} message(s): {exc.text} (code {exc.code})"
            ) from exc
    caller = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    client = await slack_web_client(runtime, team_id=team_id)
    try:
        for user_id in dict.fromkeys((caller, recipient_id)):
            info = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]
            user = cast(dict[str, object], info["user"])
            if (
                user.get("id") != user_id
                or user.get("team_id") != team_id
                or user.get("deleted")
                or user.get("is_bot")
                or user.get("is_stranger")
            ):
                raise ToolError(
                    "sender and recipient must be active human members of this workspace"
                )
        opened = await client.conversations_open(users=recipient_id)  # pyright: ignore[reportUnknownMemberType]
        channel = cast(dict[str, str], opened["channel"])["id"]
        for chunk in chunks:
            response = await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                text=chunk,
                mrkdwn=False,
                parse="none",
                link_names=False,
                unfurl_links=False,
                unfurl_media=False,
            )
            ids.append(cast(str, response["ts"]))
        return DirectMessageResult(
            platform="slack",
            recipient_id=recipient_id,
            channel_id=channel,
            message_ids=ids,
        )
    except SlackApiError as exc:
        error = cast(str, exc.response["error"])
        if error == "missing_scope":
            error = (
                "this workspace's daimon install is missing a required Slack scope "
                "(direct messages require im:write); a workspace admin must reinstall "
                "or reauthorize daimon from the install link"
            )
        elif error in {"token_revoked", "token_expired", "invalid_auth"}:
            error = (
                "this workspace's Slack authorization is no longer valid; "
                "a workspace admin must reinstall or reauthorize daimon from the install link"
            )
        raise ToolError(f"Slack DM failed after {len(ids)} message(s): {error}") from exc
