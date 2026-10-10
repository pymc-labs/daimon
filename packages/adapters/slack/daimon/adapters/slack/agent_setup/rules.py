"""Permissions clicks from Who answers where: who can read the channel, who can post.

Workspace admins only, re-checked by the dispatcher. The rules live in
`daimon.core.channel_rules`; this module supplies Slack's channel name and
words the outcome.
"""

from __future__ import annotations

import functools
import uuid
from typing import Any, cast

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.access_policy import ChannelReaders, ChannelWriters
from daimon.core.authz import build_subject
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_rules import ChannelRuleRefused, set_channel_rule
from daimon.core.errors import DaimonError
from daimon.core.panel_audit import record_panel_write
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

RULE_NEED_ADMIN_MESSAGE = (
    "Only a workspace admin can change who reads or posts in a channel. Nothing changed."
)


async def _channel_name(client: AsyncWebClient, channel_id: str) -> str | None:
    """The channel's name to call a copied agent after; None when it can't be read."""
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
    except SlackApiError:
        return None
    name = cast("dict[str, Any]", info.get("channel") or {}).get("name")
    return name if isinstance(name, str) else None


async def change_rule(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    channel_id: str,
    readers: ChannelReaders | None = None,
    writers: ChannelWriters | None = None,
    copy: bool = False,
    release: bool = False,
) -> str:
    """Apply one click and return what happened, worded for the admin who clicked."""
    audit = functools.partial(
        record_panel_write,
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id=user_id,
        op="channel_rule",
    )
    try:
        channel, _, _ = normalize_channel_admin_ids(
            "slack", channel_id=channel_id, role_ids=(), user_ids=()
        )
    except InvalidChannelAdminIds as exc:
        await audit(outcome="error", reason="invalid_channel")
        return f"{exc}. Nothing changed."
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )
    public_url = runtime.settings.mcp.public_url
    try:
        change = await set_channel_rule(
            runtime.anthropic,
            runtime.sessionmaker,
            tenant_id=tenant_id,
            platform="slack",
            channel_id=channel,
            readers=readers,
            writers=writers,
            # Only a workspace admin reaches the permissions controls.
            subject=build_subject(is_admin=True, platform_user_id=user_id),
            default=runtime.deployment_default,
            actor_account_id=actor.account_id,
            copy=copy,
            channel_label=await _channel_name(client, channel) if copy else None,
            public_url=str(public_url) if public_url is not None else None,
            release_agents=release,
        )
    except ChannelRuleRefused as exc:
        await audit(outcome="denied", reason=f"rule:{exc.reason}")
        return f"{exc} Nothing changed."
    except DaimonError as exc:  # a copy that can't be made
        await audit(outcome="error", reason="failed")
        return f"{exc} Nothing changed."
    await audit(outcome="allowed", reason="completed")
    return f"<#{channel}>: " + "\n\n".join(escape_mrkdwn(note) for note in change.notes)
