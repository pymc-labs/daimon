"""Channel isolation clicks from Who answers where: isolate, isolate with a copy, or end.

Workspace admins only, re-checked by the dispatcher. The rules live in
`daimon.core.channel_isolation_setup`; this module supplies Slack's fork and
channel name and words the outcome.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, cast

from daimon.adapters.slack.agent_setup.write import (
    _build_fork_fernet,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_fork import fork_agent
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.channel_isolation_setup import (
    ChannelIsolationRefused,
    ForkAgent,
    set_channel_isolation,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

IsolationChoice = Literal["isolate", "copy", "end"]

ISOLATION_NEED_ADMIN_MESSAGE = "Only a workspace admin can isolate a channel. Nothing changed."


def _fork(runtime: SlackRuntime, tenant_id: uuid.UUID) -> ForkAgent:
    public_url = runtime.settings.mcp.public_url

    async def fork(source: str, new_name: str) -> None:
        await fork_agent(
            runtime.anthropic,
            runtime.sessionmaker,
            tenant_id=tenant_id,
            source_name=source,
            new_name=new_name,
            public_url=str(public_url) if public_url is not None else None,
            fernet=_build_fork_fernet(runtime),
            oauth_scopes=tuple(runtime.settings.github.oauth_scopes),
        )

    return fork


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
    try:
        change = await set_channel_isolation(
            runtime.anthropic,
            runtime.sessionmaker,
            tenant_id=tenant_id,
            channel_id=channel,
            isolated=choice != "end",
            default=runtime.deployment_default,
            actor_account_id=actor.account_id,
            channel_label=await _channel_name(client, channel) if copy else None,
            fork=_fork(runtime, tenant_id) if copy else None,
        )
    except ChannelIsolationRefused as exc:
        return f"{exc} Nothing changed."
    if not change.isolated:
        return f"<#{channel}> is open again."
    name = escape_mrkdwn(change.agent_name or "")
    if change.forked_from is not None:
        source = escape_mrkdwn(change.forked_from)
        return f"<#{channel}> is isolated. *{name}*, a copy of *{source}*, answers only there."
    return f"<#{channel}> is isolated. *{name}* answers only there."
