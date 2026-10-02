"""Channel isolation clicks from Who answers where: isolate, with a copy, end, or lift.

Workspace admins only, re-checked by the dispatcher. The rules live in
`daimon.core.channel_isolation_setup`; this module supplies Slack's channel
name and words the outcome.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, cast

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.authz import build_subject
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_isolation_setup import set_channel_isolation
from daimon.core.errors import DaimonError
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

IsolationChoice = Literal["isolate", "copy", "end", "lift"]
"""`lift` ends isolation and lifts the channel's seal and dedicated pins too."""

ISOLATION_NEED_ADMIN_MESSAGE = "Only a workspace admin can isolate a channel. Nothing changed."


async def _channel_name(client: AsyncWebClient, channel_id: str) -> str | None:
    """The channel's name to call a copied agent after; None when it can't be read."""
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
    except SlackApiError:
        return None
    name = cast("dict[str, Any]", info.get("channel") or {}).get("name")
    return name if isinstance(name, str) else None


async def change_isolation(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    channel_id: str,
    choice: IsolationChoice,
) -> str:
    """Apply one click and return what happened, worded for the admin who clicked."""
    try:
        channel, _, _ = normalize_channel_admin_ids(
            "slack", channel_id=channel_id, role_ids=(), user_ids=()
        )
    except InvalidChannelAdminIds as exc:
        return f"{exc}. Nothing changed."
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )
    copy = choice == "copy"
    public_url = runtime.settings.mcp.public_url
    try:
        change = await set_channel_isolation(
            runtime.anthropic,
            runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="slack",
            channel_id=channel,
            isolated=choice in ("isolate", "copy"),
            default=runtime.deployment_default,
            actor_account_id=actor.account_id,
            channel_label=await _channel_name(client, channel) if copy else None,
            fork=copy,
            public_url=str(public_url) if public_url is not None else None,
            drop_seal_and_pins=choice == "lift",
            # Only a workspace admin reaches the isolation buttons.
            subject=build_subject(is_admin=True, platform_user_id=user_id),
        )
    except DaimonError as exc:  # a refusal, or a copy that can't be made
        return f"{exc} Nothing changed."
    if not change.isolated:
        return f"<#{channel}> is no longer isolated. {change.end_warning}"
    name = escape_mrkdwn(change.agent_name or "")
    if change.forked_from is not None:
        source = escape_mrkdwn(change.forked_from)
        copied = f"<#{channel}> is isolated. *{name}*, a copy of *{source}*, answers only there."
        note = change.dropped_skills_note
        return f"{copied} {escape_mrkdwn(note)}" if note else copied
    return f"<#{channel}> is isolated. *{name}* answers only there."
