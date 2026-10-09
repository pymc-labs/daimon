"""Ephemeral Slack /here status card."""

from __future__ import annotations

from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_details import GitHubDeploymentFacts
from daimon.core.errors import DaimonError
from daimon.core.here_card import HereCard, load_here_card, render_here_card
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import find_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient

NOTIFICATION_TEXT = "Channel status"


async def _channels(client: AsyncWebClient, *, user_id: str | None, types: str) -> dict[str, bool]:
    """Collect every channel in a bot or caller membership listing."""
    result: dict[str, bool] = {}
    cursor: str | None = None
    while True:
        kwargs: dict[str, Any] = {"types": types, "limit": 200, "cursor": cursor}
        if user_id is not None:
            kwargs["user"] = user_id
        response = await client.users_conversations(**kwargs)  # pyright: ignore[reportUnknownMemberType]
        result.update(
            (str(row["id"]), bool(row.get("is_private")))
            for row in cast(list[dict[str, Any]], response.get("channels") or [])
        )
        metadata = cast(dict[str, str], response.get("response_metadata") or {})
        cursor = metadata.get("next_cursor") or None
        if cursor is None:
            return result


async def _visible_channel_ids(client: AsyncWebClient, user_id: str) -> set[str]:
    """Bot channels the caller may see, including public channels for members."""
    bot_channels = await _channels(client, user_id=None, types="public_channel,private_channel")
    info = await client.users_info(user=user_id)  # pyright: ignore[reportUnknownMemberType]
    user = cast(dict[str, Any], info.get("user") or {})
    guest = bool(user.get("is_restricted") or user.get("is_ultra_restricted"))
    member_types = "public_channel,private_channel" if guest else "private_channel"
    member_ids = await _channels(client, user_id=user_id, types=member_types)
    return {
        channel_id
        for channel_id, is_private in bot_channels.items()
        if (not is_private and not guest) or channel_id in member_ids
    }


def build_here_attachment(card: HereCard) -> dict[str, Any]:
    """Block Kit inside a coloured attachment, with no name interpolation in mrkdwn."""
    shown = render_here_card(card)
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": shown.title}},
    ]
    if shown.subline is not None:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": shown.subline}})
    fields: list[dict[str, str]] = []
    if shown.reading is not None:
        fields.append({"type": "mrkdwn", "text": f"*Reading*\n{shown.reading}"})
    if shown.publishing is not None:
        fields.append({"type": "mrkdwn", "text": f"*Publishing*\n{shown.publishing}"})
    if fields:
        blocks.append({"type": "section", "fields": fields})
    if shown.extras:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "plain_text", "text": "\n".join(shown.extras)}],
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "plain_text", "text": "Channel setting. Threads can differ."}],
        }
    )
    return {"color": shown.colour, "fallback": card.text, "blocks": blocks}


async def _setter_display_name(client: AsyncWebClient, external_id: str) -> str | None:
    """Resolve the setter to a plain name; never return a Slack mention."""
    try:
        response = await client.users_info(user=external_id)  # pyright: ignore[reportUnknownMemberType]
    except SlackApiError:
        return None
    user = cast(dict[str, Any], response.get("user") or {})
    profile = cast(dict[str, Any], user.get("profile") or {})
    return (
        str(profile.get("display_name") or user.get("real_name") or user.get("name") or "") or None
    )


async def handle_here_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Read the invoking channel and post one status card visible only to its caller."""
    team_id = str(payload.get("team_id") or "")
    channel_id = str(payload.get("channel_id") or "")
    user_id = str(payload.get("user_id") or "")
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    try:
        try:
            info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
            channel = dict(info.get("channel") or {})  # pyright: ignore[reportUnknownArgumentType]
            bot_view = bool(channel.get("is_member"))
        except SlackApiError as exc:
            error = str(cast(dict[str, Any], exc.response.data).get("error", ""))  # pyright: ignore[reportUnknownMemberType]
            if error not in {"channel_not_found", "not_in_channel"}:
                raise
            bot_view = False
        caller_view = True  # Slack delivered this command from the caller's channel.
        visible_ids = await _visible_channel_ids(client, user_id)
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
                thread_id=None,
                channel_level_only=True,
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
                resolve_setter_display=lambda external_id: _setter_display_name(
                    client, external_id
                ),
                visible_channel_ids=visible_ids,
                bot_can_view=bot_view,
                caller_can_view=caller_view,
            )
        response_url = str(payload.get("response_url") or "")
        # The attachment carries the card; top-level text is only the notification
        # line, so Slack does not render the card twice.
        if not bot_view and response_url:
            await AsyncWebhookClient(response_url).send_dict(
                {
                    "text": NOTIFICATION_TEXT,
                    "attachments": [build_here_attachment(card)],
                    "response_type": "ephemeral",
                    "parse": "none",
                }
            )
        else:
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=NOTIFICATION_TEXT,
                attachments=[build_here_attachment(card)],
                parse="none",
                link_names=False,
            )
    except (SlackApiError, DaimonError) as exc:
        await surface_command_error(
            client,
            exc,
            request_id=generate_request_id(),
            title="Here",
            view_id="",
            channel_id=channel_id,
            user_id=user_id,
        )
