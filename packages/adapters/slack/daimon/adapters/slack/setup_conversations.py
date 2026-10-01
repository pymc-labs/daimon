"""Slack setup conversation creation and lifecycle, without executing agent turns."""

from __future__ import annotations

import contextlib
from typing import Any

import aiohttp
from daimon.adapters.slack.admin import ADMIN_NOUN, resolve_is_admin
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.setup_conversations import (
    SETUP_ACTION_LABEL,
    build_setup_opener,
    resolve_setup_agents,
    setup_thread_name,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.thread_agent_bindings import (
    create_binding,
    update_channel_lifecycle,
    update_lifecycle,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


def setup_button(target_ma_agent_id: str | None) -> dict[str, Any]:
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "agent_setup__conversation",
                "text": {"type": "plain_text", "text": SETUP_ACTION_LABEL},
                "value": target_ma_agent_id or "choose",
            }
        ],
    }


def setup_link(team_id: str, channel_id: str, thread_id: str) -> str:
    return f"https://app.slack.com/client/{team_id}/{channel_id}/thread/{channel_id}-{thread_id}"


def setup_reply_button(link: str) -> dict[str, object]:
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": "setup_conversation_reply",
                "text": {"type": "plain_text", "text": "Reply to Daimon"},
                "style": "primary",
                "url": link,
            }
        ],
    }


async def create_setup_conversation(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    channel_id: str,
    user_id: str,
    target_ma_agent_id: str | None,
) -> str:
    """Validate immutable identities, persist a public root, then announce readiness."""
    if not channel_id.startswith(("C", "G")):
        raise DaimonError("Open /agent-setup in a workspace channel to start a setup conversation.")
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    responder, target = await resolve_setup_agents(
        runtime.anthropic, tenant_id=tenant_id, target_ma_agent_id=target_ma_agent_id
    )
    auth = await client.auth_test()  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
    bot_user_id = str(auth.get("user_id") or "")
    if not bot_user_id:
        raise DaimonError("Slack did not identify the bot. Retry opening setup.")
    target_name = str(target.metadata.get(MA_METADATA_KEY_NAME) or target.name) if target else None
    async with runtime.sessionmaker.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
    opener = build_setup_opener(
        target_display=escape_mrkdwn(target_name) if target_name else None,
        bot_mention=f"<@{bot_user_id}>",
        is_admin=await resolve_is_admin(client, user_id=user_id),
        admin_noun=ADMIN_NOUN,
    )
    posted = await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
        channel=channel_id, text="Starting a thread with Daimon…"
    )
    thread_id = str(posted.get("ts") or "")
    if not thread_id:
        raise DaimonError("Slack did not return the setup message identity. Please retry.")
    opener_ts: str | None = None
    try:
        async with runtime.sessionmaker.begin() as session:
            await create_binding(
                session,
                tenant_id=tenant_id,
                platform="slack",
                parent_channel_id=channel_id,
                thread_id=thread_id,
                responder_ma_agent_id=str(responder.id),
                responder_name=str(responder.metadata.get(MA_METADATA_KEY_NAME) or responder.name),
                configuration_target_ma_agent_id=str(target.id) if target else None,
                configuration_target_name=target_name,
                creator_account_id=principal.account_id,
            )
        reply = await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
            channel=channel_id,
            thread_ts=thread_id,
            text=opener,
        )
        opener_ts = str(reply.get("ts") or "")
        if not opener_ts:
            raise DaimonError("Slack did not return the setup reply identity. Please retry.")
        permalink = await client.chat_getPermalink(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
            channel=channel_id, message_ts=opener_ts
        )
        link = str(permalink.get("permalink") or "")
        if not link:
            raise DaimonError(
                "Slack did not return a link to the setup conversation. Please retry."
            )
        heading = setup_thread_name(escape_mrkdwn(target_name) if target_name else None)
        launcher = f"{heading}\nOpen this thread to reply to Daimon."
        await client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
            channel=channel_id,
            ts=thread_id,
            text=f"{launcher}\n<{link}|Reply to Daimon>",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": launcher}},
                setup_reply_button(link),
            ],
        )
    except Exception:
        for message_ts in (opener_ts, thread_id):
            if not message_ts:
                continue
            try:
                await client.chat_delete(channel=channel_id, ts=message_ts)  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
            except (SlackApiError, aiohttp.ClientError, TimeoutError):
                with contextlib.suppress(SlackApiError, aiohttp.ClientError, TimeoutError):
                    await client.chat_update(  # pyright: ignore[reportUnknownMemberType]  # SDK kwargs
                        channel=channel_id,
                        ts=message_ts,
                        text="Setup failed. Reopen /agent-setup to try again.",
                        blocks=[],
                    )
        async with runtime.sessionmaker.begin() as session:
            await update_lifecycle(
                session,
                tenant_id=tenant_id,
                platform="slack",
                parent_channel_id=channel_id,
                thread_id=thread_id,
                deleted=True,
            )
        raise
    return link


async def handle_setup_lifecycle(
    runtime: SlackRuntime, event: dict[str, Any], *, team_id: str
) -> None:
    """Only change binding state; ordinary subscribed messages never run turns."""
    event_type = str(event.get("type") or "")
    channel_id = str(event.get("channel") or "")
    if not team_id or not channel_id:
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker.begin() as session:
        if event_type == "message" and event.get("subtype") == "message_deleted":
            deleted_ts = str(event.get("deleted_ts") or "")
            if deleted_ts:
                await update_lifecycle(
                    session,
                    tenant_id=tenant_id,
                    platform="slack",
                    parent_channel_id=channel_id,
                    thread_id=deleted_ts,
                    deleted=True,
                )
        elif event_type in {
            "channel_archive",
            "channel_unarchive",
            "channel_deleted",
            "group_archive",
            "group_unarchive",
            "group_deleted",
        }:
            await update_channel_lifecycle(
                session,
                tenant_id=tenant_id,
                platform="slack",
                parent_channel_id=channel_id,
                archived=event_type.endswith("_archive")
                if not event_type.endswith("_deleted")
                else None,
                deleted=True if event_type.endswith("_deleted") else None,
            )
