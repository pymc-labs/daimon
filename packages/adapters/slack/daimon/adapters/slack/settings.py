"""Slack `/settings`: the shared Settings panel (`daimon.core.scope_settings`) for the
channel it is run in, for its caller only. The channel and the person come from Slack's
signed envelope, never from the pick. Slash commands do not say which thread they were
run in, so there is no thread level here; a thread is changed by asking Daimon in it.
"""

from __future__ import annotations

from typing import Any, Final, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope_settings import (
    SettingsActor,
    SettingsPanel,
    SettingsPlace,
    apply_choice,
    chat_actor,
    load_panel,
)
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

ACTION_ID: Final = "settings_change"
NOTIFICATION_TEXT: Final = "Settings"
IN_A_CHANNEL: Final = "Settings belong to a channel. Run `/settings` in a channel."


def build_settings_blocks(panel: SettingsPanel) -> list[dict[str, Any]]:
    """The panel as Block Kit: a header line, then each row with its select."""
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{panel.title}*\n{panel.subline}"}}
    ]
    for row in panel.rows:
        section: dict[str, Any] = {
            "type": "section",
            "block_id": f"settings_{row.key}",
            "text": {"type": "mrkdwn", "text": f"*{row.name}*\n{row.summary}"},
        }
        if row.choices:
            section["accessory"] = {
                "type": "static_select",
                "action_id": ACTION_ID,
                "placeholder": {"type": "plain_text", "text": row.placeholder[:150]},
                "options": [
                    {"text": {"type": "plain_text", "text": label[:75]}, "value": value}
                    for value, label in row.choices
                ],
            }
        blocks.append(section)
    return blocks


async def _actor(
    runtime: SlackRuntime, client: AsyncWebClient, team_id: str, user_id: str
) -> SettingsActor:
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
        panel = await load_panel(
            runtime.sessionmaker,
            runtime.settings,
            await _actor(runtime, client, team_id, user_id),
            SettingsPlace.of("slack", channel_id, None),
        )
        await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=NOTIFICATION_TEXT,
            blocks=build_settings_blocks(panel),
            parse="none",
            link_names=False,
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
    """A pick from the panel: place and person from the signed envelope; re-checked on write."""
    team_id, user_id, channel_id = (
        str(cast(dict[str, Any], payload.get(part) or {}).get("id") or "")
        for part in ("team", "user", "channel")
    )
    actions = cast(list[dict[str, Any]], payload.get("actions") or [{}])
    raw = cast(dict[str, Any], actions[0].get("selected_option") or {}).get("value")
    if not team_id or not user_id or not channel_id or channel_id.startswith("D"):
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    done = await apply_choice(
        runtime.sessionmaker,
        runtime.settings,
        await _actor(runtime, client, team_id, user_id),
        raw,
        SettingsPlace.of("slack", channel_id, None),
    )
    if done is not None:
        await post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=done[1], error=not done[0]
        )
