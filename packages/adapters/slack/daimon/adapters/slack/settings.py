"""Slack `/settings`: the shared panel (`daimon.core.settings_panel`) for the channel it is
run in, for its caller only. A pick replaces the message with the next screen. The channel
and the person come from Slack's signed envelope, never from the pick. Slash commands do
not say which thread they were run in, so there is no thread level here; a thread is
changed on the web or by asking Daimon in it.
"""

from __future__ import annotations

from typing import Any, Final, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.blockkit import notice_attachments
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import DaimonError
from daimon.core.features.service import FeatureActor
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.settings_panel import PanelScreen, chat_actor, open_panel, press, tenant_agents
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.webhook.async_client import AsyncWebhookClient

ACTION_ID: Final = "settings_change"
NOTIFICATION_TEXT: Final = "Settings"
IN_A_CHANNEL: Final = "Settings belong to a channel. Run `/settings` in a channel."


def is_settings_action(action_id: str) -> bool:
    """Whether a block action is a press on the settings panel, a paging button included."""
    return action_id == ACTION_ID or action_id.startswith(f"{ACTION_ID}:")


def _text(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def build_settings_blocks(screen: PanelScreen) -> list[dict[str, Any]]:
    """A screen as Block Kit: its title and lines, a select per control, a Back button and,
    on a long list, Previous and Next."""
    blocks = [_text(f"*{screen.title}*\n" + "\n".join(screen.lines))]
    for index, control in enumerate(screen.selects):
        select: dict[str, Any] = {
            "type": "static_select",
            "action_id": ACTION_ID,
            "placeholder": {"type": "plain_text", "text": control.placeholder[:150]},
            "options": [
                {"text": {"type": "plain_text", "text": o.label[:75]}, "value": o.value}
                | (
                    {"description": {"type": "plain_text", "text": o.description[:75]}}
                    if o.description
                    else {}
                )
                for o in control.options
            ],
        }
        blocks.append({"type": "actions", "block_id": f"settings_{index}", "elements": [select]})
    buttons = [(ACTION_ID, screen.back)] if screen.back is not None else []
    buttons += [(f"{ACTION_ID}:{n}", option) for n, option in enumerate(screen.paging)]
    if buttons:
        elements = [
            {
                "type": "button",
                "action_id": action_id,
                "text": {"type": "plain_text", "text": option.label},
                "value": option.value,
            }
            for action_id, option in buttons
        ]
        blocks.append({"type": "actions", "block_id": "settings_back", "elements": elements})
    return blocks


def screen_fields(screen: PanelScreen) -> dict[str, Any]:
    """The message fields for a screen: its blocks, and the result of a change as a notice."""
    fields: dict[str, Any] = {"text": NOTIFICATION_TEXT, "blocks": build_settings_blocks(screen)}
    if screen.notice:
        fields["attachments"] = notice_attachments(screen.notice, neutral=screen.ok)
    return fields


async def _actor(
    runtime: SlackRuntime, client: AsyncWebClient, team_id: str, user_id: str
) -> FeatureActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return await resolve_is_admin(client, user_id=user_id, host_team_id=team_id, action=action)

    return await chat_actor(
        runtime.sessionmaker,
        tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
        platform="slack",
        user_id=user_id,
        admin_for=admin_for,
    )


async def handle_settings_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Post the channel's settings panel visible only to its caller."""
    team_id, channel_id, user_id = (
        str(payload.get(name) or "") for name in ("team_id", "channel_id", "user_id")
    )
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    try:
        if not channel_id or channel_id.startswith("D"):
            raise ChannelChangeRefused(IN_A_CHANNEL, kind="invalid")
        actor = await _actor(runtime, client, team_id, user_id)
        screen = await open_panel(
            runtime.sessionmaker,
            runtime.settings,
            actor,
            FeaturePlace.of(actor.tenant_id, "slack", channel_id, None),
            agents=tenant_agents(runtime.anthropic, actor.tenant_id),
        )
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            parse="none",
            link_names=False,
            **screen_fields(screen),
        )
    except (SlackApiError, DaimonError) as exc:
        await surface_command_error(
            client,
            exc,
            request_id=generate_request_id(),
            title=NOTIFICATION_TEXT,
            view_id="",
            channel_id=channel_id,
            user_id=user_id,
        )


async def handle_settings_change(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """A pick from the panel: place and person from the signed envelope; re-checked on write.
    The message becomes the screen the pick leads to."""
    team_id, user_id, channel_id = (
        str(cast(dict[str, Any], payload.get(part) or {}).get("id") or "")
        for part in ("team", "user", "channel")
    )
    element = cast(list[dict[str, Any]], payload.get("actions") or [{}])[0]
    raw = cast(dict[str, Any], element.get("selected_option") or {}).get("value") or element.get(
        "value"
    )
    response_url = str(payload.get("response_url") or "")
    if not team_id or not user_id or not channel_id or channel_id.startswith("D"):
        return
    if not response_url:
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    actor = await _actor(runtime, client, team_id, user_id)
    screen = await press(
        runtime.sessionmaker,
        runtime.settings,
        actor,
        raw,
        FeaturePlace.of(actor.tenant_id, "slack", channel_id, None),
        agents=tenant_agents(runtime.anthropic, actor.tenant_id),
    )
    if screen is not None:
        await AsyncWebhookClient(response_url).send_dict(
            {"replace_original": True, "parse": "none", **screen_fields(screen)}
        )
