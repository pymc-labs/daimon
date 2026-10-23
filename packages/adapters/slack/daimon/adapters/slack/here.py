"""Ephemeral Slack /here status card."""

from __future__ import annotations

from typing import Any, cast

from aiohttp import ClientError
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.outsiders import channel_outsiders
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_details import GitHubDeploymentFacts
from daimon.core.errors import DaimonError
from daimon.core.here_card import HereCard, load_here_card, render_here_card, render_here_card_text
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.outsiders import OutsiderVisibility
from daimon.core.permissions import channel_permissions
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.identity import find_platform_principal
from slack_sdk.errors import SlackApiError, SlackClientError
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


def build_here_attachment(card: HereCard, *, is_admin: bool = False) -> dict[str, Any]:
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
    if is_admin and card.outsiders_can_view and card.effective_readers != "own":
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "here_make_confidential",
                        "text": {"type": "plain_text", "text": "Make confidential"},
                    }
                ],
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "plain_text", "text": "Channel setting. Threads can differ."}],
        }
    )
    return {"color": shown.colour, "fallback": card.text, "blocks": blocks}


def build_visibility_attachment(visibility: OutsiderVisibility, *, readers: str) -> dict[str, Any]:
    """Fixed current-channel facts when optional Slack metadata is unavailable."""
    lines = (
        "Some channel details are unavailable. Try again shortly.",
        *visibility.lines(readers=readers),
    )
    return {
        "color": "#F0B429",
        "fallback": "\n".join((NOTIFICATION_TEXT, *lines)),
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": NOTIFICATION_TEXT}},
            {"type": "section", "text": {"type": "plain_text", "text": "\n".join(lines)}},
        ],
    }


async def _post_here_attachment(
    client: AsyncWebClient,
    payload: dict[str, Any],
    attachment: dict[str, Any],
    *,
    bot_view: bool | None,
) -> None:
    response_url = str(payload.get("response_url") or "")
    if not bot_view and response_url:
        await AsyncWebhookClient(response_url).send_dict(
            {
                "text": NOTIFICATION_TEXT,
                "attachments": [attachment],
                "response_type": "ephemeral",
                "parse": "none",
            }
        )
    else:
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=str(payload.get("channel_id") or ""),
            user=str(payload.get("user_id") or ""),
            text=NOTIFICATION_TEXT,
            attachments=[attachment],
            parse="none",
            link_names=False,
        )


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
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        visibility = await channel_outsiders(client, team_id=team_id, channel_id=channel_id)
        bot_view = visibility.bot_can_view
        try:
            if bot_view is None:
                raise SlackClientError("Channel metadata is unavailable")
            is_admin = await resolve_is_admin(client, user_id=user_id)
            visible_ids = await _visible_channel_ids(client, user_id)
            visible_ids.add(channel_id)
        except (SlackClientError, ClientError, TimeoutError):
            async with runtime.sessionmaker() as session:
                policy = await load_access_policy(session, tenant_id=tenant_id)
            # The signed slash payload establishes this channel only. Do not
            # load routing/agent details or offer admin controls on this path.
            attachment = build_visibility_attachment(
                visibility, readers=channel_permissions(policy, channel_id=channel_id).readers
            )
            await _post_here_attachment(client, payload, attachment, bot_view=bot_view)
            return
        caller_view = True  # Slack delivered this command from the caller's channel.
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
                    app_configured=runtime.settings.github_app.app_id is not None
                    and runtime.settings.github_app.private_key is not None,
                ),
                public_mcp_url=str(runtime.settings.mcp.public_url)
                if runtime.settings.mcp.public_url is not None
                else None,
                is_admin=is_admin,
                caller_account_id=principal.account_id if principal else None,
                resolve_setter_display=lambda external_id: _setter_display_name(
                    client, external_id
                ),
                visible_channel_ids=visible_ids,
                bot_can_view=bot_view,
                caller_can_view=caller_view,
            )
        card = card.model_copy(
            update={
                "outsiders_can_view": visibility.outsiders_can_view,
                "outsider_note": visibility.note,
            }
        )
        card = card.model_copy(update={"text": render_here_card_text(card)})
        await _post_here_attachment(
            client, payload, build_here_attachment(card, is_admin=is_admin), bot_view=bot_view
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


async def handle_make_confidential(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Use Slack's signed channel context and re-check the current workspace admin."""
    from daimon.adapters.slack.agent_setup.rules import RULE_NEED_ADMIN_MESSAGE, change_rule

    team = cast(dict[str, Any], payload.get("team") or {})
    channel = cast(dict[str, Any], payload.get("channel") or {})
    user = cast(dict[str, Any], payload.get("user") or {})
    team_id, channel_id, user_id = (
        str(team.get("id") or ""),
        str(channel.get("id") or ""),
        str(user.get("id") or ""),
    )
    if not team_id or not channel_id or not user_id:
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    if not await resolve_is_admin(client, user_id=user_id):
        text = RULE_NEED_ADMIN_MESSAGE
    else:
        text = await change_rule(
            runtime,
            client,
            tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
            user_id=user_id,
            channel_id=channel_id,
            readers="own",
        )
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id,
        user=user_id,
        text=text,
        parse="none",
    )
