"""Private Slack cards for newly installed GitHub repos."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.github_card_ui import github_card_blocks
from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_notice_visibility import new_repo_notice_copy, visible_new_repo_names
from daimon.core.github_panel import connect_link, sync_connect_admin
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_connect import admin_account_for_platform_user
from daimon.core.stores.github_new_repo_notices import (
    claim_notice_group,
    dismiss_notice,
    finish_notice,
    notices_for_day,
)
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
        group = await claim_notice_group(session, tenant_id=tenant_id, now=datetime.now(UTC))
    if group is None:
        return
    visible_names: tuple[str, ...] = ()
    if runtime.settings.crypto.keys:
        async with runtime.sessionmaker() as session:
            try:
                account_id = await admin_account_for_platform_user(
                    session, tenant_id=tenant_id, external_id=user_id
                )
            except ValueError:
                account_id = None
            if account_id is not None:
                fernet = build_multifernet(
                    tuple(key.get_secret_value() for key in runtime.settings.crypto.keys)
                )
                visible_names = await visible_new_repo_names(
                    session,
                    group=group,
                    account_id=account_id,
                    platform="slack",
                    platform_user_id=user_id,
                    fernet=fernet,
                    http_client=runtime.http_client,
                )
    copy = new_repo_notice_copy(visible_names)
    value = json.dumps(
        {"day": group.notices[0].queued_at.strftime("%Y%m%d")}, separators=(",", ":")
    )
    text = copy.text
    buttons = [
        {
            "type": "button",
            "action_id": ACTION_CONNECT,
            "text": {"type": "plain_text", "text": copy.connect_label},
            "value": value,
        }
    ]
    if copy.dismiss_label is not None:
        buttons.append(
            {
                "type": "button",
                "action_id": ACTION_DISMISS,
                "text": {"type": "plain_text", "text": copy.dismiss_label},
                "value": value,
            }
        )
    blocks = github_card_blocks(text, buttons=buttons)
    try:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id, user=user_id, text=text, blocks=blocks
        )
    except Exception:
        async with runtime.sessionmaker.begin() as session:
            for notice in group.notices:
                await finish_notice(session, notice=notice, delivered=False, now=datetime.now(UTC))
        return
    async with runtime.sessionmaker.begin() as session:
        for notice in group.notices:
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
    message = cast("dict[str, Any]", payload.get("message") or {})
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
        card_value: object = json.loads(str(action.get("value") or ""))
    except (ValueError, TypeError):
        return
    if not isinstance(card_value, dict):
        return
    day: object = cast("dict[str, object]", card_value).get("day")
    if not isinstance(day, str) or len(day) != 8 or not day.isdecimal():
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    url: str | None = None
    try:
        async with runtime.sessionmaker.begin() as session:
            await sync_connect_admin(
                session,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                verified_tenant_admin=True,
            )
            account_id = await admin_account_for_platform_user(
                session, tenant_id=tenant_id, external_id=user_id
            )
            group = await notices_for_day(session, tenant_id=tenant_id, day=day)
            if group is None:
                return
            entries: list[tuple[int, str]] = []
            if runtime.settings.crypto.keys:
                fernet = build_multifernet(
                    tuple(key.get_secret_value() for key in runtime.settings.crypto.keys)
                )
                visible = await visible_new_repo_names(
                    session,
                    group=group,
                    account_id=account_id,
                    platform="slack",
                    platform_user_id=user_id,
                    fernet=fernet,
                    http_client=runtime.http_client,
                )
                entries = [
                    (notice.installation_id, notice.repo_full_name)
                    for notice in group.notices
                    if notice.repo_full_name in visible
                ]
            if action_id == ACTION_CONNECT:
                await sync_connect_admin(
                    session,
                    tenant_id=tenant_id,
                    platform="slack",
                    platform_user_id=user_id,
                    verified_tenant_admin=await resolve_is_admin(client, user_id=user_id),
                )
                url = await connect_link(
                    session,
                    settings=runtime.settings,
                    tenant_id=tenant_id,
                    platform="slack",
                    platform_user_id=user_id,
                    verified_tenant_admin=await resolve_is_admin(client, user_id=user_id),
                    workspace_label=str(team.get("name") or "") or None,
                    requester_label=str(user.get("name") or "") or None,
                    origin_parent_channel_id=channel_id,
                    origin_thread_id=str(message.get("thread_ts") or "") or None,
                    origin_followup_token=str(payload.get("response_url") or "") or None,
                    origin_followup_expires_at=(
                        datetime.now(UTC) + timedelta(minutes=30)
                        if payload.get("response_url")
                        else None
                    ),
                )
            else:
                for installation_id, repo_name in entries:
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
        await send_link(
            client,
            channel_id=channel_id,
            thread_id=str(message.get("thread_ts") or "") or None,
            user_id=user_id,
            url=url,
        )
    else:
        reply = "Connect later: /github → Connect more repos."
        await post_ephemeral(client, channel_id=channel_id, user_id=user_id, text=reply)
