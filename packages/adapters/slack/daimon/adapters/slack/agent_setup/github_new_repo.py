"""Private Slack cards for newly installed GitHub repos."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import CONNECT_COPY, connect_link
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import admin_account_for_platform_user
from daimon.core.stores.github_new_repo_notices import (
    claim_next_notice,
    dismiss_notice,
    finish_notice,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.security_audit import append_event
from slack_sdk.web.async_client import AsyncWebClient

ACTION_CONNECT = "github_new_repo__connect"
ACTION_DISMISS = "github_new_repo__dismiss"


async def send_pending_notice(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    channel_id: str,
    user_id: str,
) -> None:
    """Send one queued card to the admin who opened the setup panel."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker.begin() as session:
        notice = await claim_next_notice(session, tenant_id=tenant_id, now=datetime.now(UTC))
    if notice is None:
        return
    value = f"{notice.installation_id}:{notice.repo_full_name}"
    text = f"New repo `{notice.repo_full_name}` in the GitHub installation — connect it?"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": ACTION_CONNECT,
                    "text": {"type": "plain_text", "text": "Connect"},
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": ACTION_DISMISS,
                    "text": {"type": "plain_text", "text": "Dismiss"},
                    "value": value,
                },
            ],
        },
    ]
    try:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id, user=user_id, text=text, blocks=blocks
        )
    except Exception:
        async with runtime.sessionmaker.begin() as session:
            await finish_notice(session, notice=notice, delivered=False, now=datetime.now(UTC))
        return
    async with runtime.sessionmaker.begin() as session:
        await finish_notice(session, notice=notice, delivered=True, now=datetime.now(UTC))


async def handle_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    actions = cast("list[dict[str, Any]]", payload.get("actions") or [])
    if not actions:
        return
    action = actions[0]
    action_id = str(action.get("action_id") or "")
    if action_id not in (ACTION_CONNECT, ACTION_DISMISS):
        return
    team = cast("dict[str, Any]", payload.get("team") or {})
    channel = cast("dict[str, Any]", payload.get("channel") or {})
    user = cast("dict[str, Any]", payload.get("user") or {})
    team_id, channel_id, user_id = (
        str(team.get("id") or ""),
        str(channel.get("id") or ""),
        str(user.get("id") or ""),
    )
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None or not await resolve_is_admin(client, user_id=user_id):
        return
    try:
        installation, repo_name = str(action.get("value") or "").split(":", 1)
        installation_id = int(installation)
    except ValueError:
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    url: str | None = None
    try:
        async with runtime.sessionmaker.begin() as session:
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant_id, platform="slack", external_id=user_id
            )
            await set_role(session, principal.account_id, Role.ADMIN)
            account_id = await admin_account_for_platform_user(
                session, tenant_id=tenant_id, external_id=user_id
            )
            if action_id == ACTION_CONNECT:
                url = await connect_link(
                    session,
                    settings=runtime.settings,
                    tenant_id=tenant_id,
                    platform="slack",
                    platform_user_id=user_id,
                    preselected_repo_full_name=repo_name,
                )
            else:
                await dismiss_notice(
                    session,
                    tenant_id=tenant_id,
                    installation_id=installation_id,
                    repo_full_name=repo_name,
                    now=datetime.now(UTC),
                )
                await append_event(
                    session,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    agent_id=None,
                    platform="slack",
                    platform_user_id=user_id,
                    tool_name="github_connect",
                    operation="github_connect",
                    outcome="allowed",
                    reason="new repo notice dismissed",
                )
    except ValueError:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text="Only a workspace admin can use this card.",
        )
        return
    if action_id == ACTION_CONNECT:
        assert url is not None
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=f"{CONNECT_COPY}\n<{url}|Open GitHub>",
        )
    else:
        await post_ephemeral(client, channel_id=channel_id, user_id=user_id, text="Dismissed.")
