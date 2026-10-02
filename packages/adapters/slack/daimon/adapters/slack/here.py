"""Ephemeral Slack /here status card."""

from __future__ import annotations

from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_details import GitHubDeploymentFacts
from daimon.core.here_card import load_here_card
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import find_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


async def _shared_channel_ids(client: AsyncWebClient, user_id: str) -> set[str]:
    """Channels both the bot and caller belong to, across Slack's cursor pages."""
    result: set[str] = set()
    cursor: str | None = None
    while True:
        response = await client.users_conversations(  # pyright: ignore[reportUnknownMemberType]
            user=user_id, types="public_channel,private_channel", limit=200, cursor=cursor
        )
        result.update(
            row["id"] for row in cast(list[dict[str, str]], response.get("channels") or [])
        )
        metadata = cast(dict[str, str], response.get("response_metadata") or {})
        cursor = metadata.get("next_cursor") or None
        if cursor is None:
            return result


def _plain_blocks(card_text: str) -> list[dict[str, Any]]:
    """Keep names as plain text so a configured name cannot create a mention."""
    body = card_text.removeprefix("**Here**\n")
    return [
        {"type": "header", "text": {"type": "plain_text", "text": "Here"}},
        *(
            {"type": "section", "text": {"type": "plain_text", "text": body[i : i + 2800]}}
            for i in range(0, len(body), 2800)
        ),
    ]


async def handle_here_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Read the invoking channel and post one status card visible only to its caller."""
    team_id = str(payload.get("team_id") or "")
    channel_id = str(payload.get("channel_id") or "")
    user_id = str(payload.get("user_id") or "")
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        channel = dict(info.get("channel") or {})  # pyright: ignore[reportUnknownArgumentType]
        bot_view = bool(channel.get("is_member")) if channel.get("is_private") else True
        caller_view = True  # Slack delivered this command from the caller's channel.
        visible_ids = await _shared_channel_ids(client, user_id)
        visible_ids.add(channel_id)
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        github = runtime.settings.github
        async with runtime.sessionmaker() as session:
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform="slack", external_id=user_id
            )
            card = await load_here_card(
                session,
                runtime.anthropic,
                tenant_id=tenant_id,
                platform="slack",
                channel_id=channel_id,
                thread_id=payload.get("thread_ts") or None,
                default=runtime.deployment_default,
                github=GitHubDeploymentFacts(
                    has_fallback_pat=github.fallback_pat is not None,
                    app_configured=github.app_id is not None and github.app_private_key is not None,
                ),
                public_mcp_url=str(runtime.settings.mcp.public_url)
                if runtime.settings.mcp.public_url is not None
                else None,
                is_admin=await resolve_is_admin(client, user_id=user_id),
                caller_account_id=principal.account_id if principal else None,
                visible_channel_ids=visible_ids,
                bot_can_view=bot_view,
                caller_can_view=caller_view,
            )
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text="Here status card",
            blocks=_plain_blocks(card.text),
            parse="none",
            link_names=False,
        )
    except SlackApiError as exc:
        await surface_command_error(
            client,
            exc,
            request_id=generate_request_id(),
            title="Here",
            view_id="",
            channel_id=channel_id,
            user_id=user_id,
        )
