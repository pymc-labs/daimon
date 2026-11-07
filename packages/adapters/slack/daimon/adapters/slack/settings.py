"""Slack `/settings`: the Settings group for the channel it is run in, for its caller only.

The panel is the shared model (`daimon.core.settings_card`); a pick is applied with
`daimon.core.scope_settings.change_setting`, which re-checks who may make it. The
channel and the person come from Slack's signed envelope, never from the pick.
Slack slash commands do not say which thread they were run in, so the panel has the
workspace and channel levels; a thread's settings are changed by asking Daimon there.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.errors import generate_request_id, surface_command_error
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import DaimonError, StaleChannelValue
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope_settings import (
    SettingsActor,
    SettingsDefaults,
    SettingsPlace,
    change_setting,
    may_change_approvals,
    read_settings,
)
from daimon.core.settings_card import (
    SettingsPanel,
    build_settings_panel,
    decode_choice,
    may_change_for,
)
from daimon.core.stores.identity import find_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

ACTION_ID: Final = "settings_change"
NOTIFICATION_TEXT: Final = "Settings"
IN_A_CHANNEL: Final = "Settings belong to a channel. Run `/settings` in a channel."


def build_settings_blocks(panel: SettingsPanel) -> list[dict[str, Any]]:
    """The panel as Block Kit: a header line, then each row with its select."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{panel.title}*\n{panel.subline}"},
        }
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
                    {
                        "text": {"type": "plain_text", "text": choice.label[:75]},
                        "value": choice.value,
                    }
                    for choice in row.choices
                ],
            }
        blocks.append(section)
    return blocks


def _actor(
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    team_id: str,
    user_id: str,
    account_id: uuid.UUID | None,
) -> SettingsActor:
    async def admin_for(action: DelegatedAction | None) -> bool:
        return await resolve_is_admin(client, user_id=user_id, host_team_id=team_id, action=action)

    return SettingsActor(
        tenant_id=tenant_id,
        account_id=account_id,
        surface="panel",
        native_user_id=user_id,
        admin_for=admin_for,
    )


async def _account_id(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, user_id: str
) -> uuid.UUID | None:
    async with runtime.sessionmaker() as session:
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
    return principal.account_id if principal is not None else None


async def handle_settings_command(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    """Post the channel's settings panel visible only to its caller."""
    team_id = str(payload.get("team_id") or "")
    channel_id = str(payload.get("channel_id") or "")
    user_id = str(payload.get("user_id") or "")
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    try:
        if not channel_id or channel_id.startswith("D"):
            raise ChannelChangeRefused(IN_A_CHANNEL, kind="invalid")
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        place = SettingsPlace.of("slack", channel_id, None)
        views = await read_settings(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            place=place,
            defaults=SettingsDefaults.from_settings(runtime.settings, "slack"),
        )
        actor = _actor(
            client, tenant_id=tenant_id, team_id=team_id, user_id=user_id, account_id=None
        )
        admin = await resolve_is_admin(client, user_id=user_id, host_team_id=team_id)
        native = await may_change_approvals(runtime.sessionmaker, runtime.settings, actor, "slack")
        panel = build_settings_panel(
            views, place=place, may_change=may_change_for(admin=admin, native_admin=native)
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
    team = cast(dict[str, Any], payload.get("team") or {})
    user = cast(dict[str, Any], payload.get("user") or {})
    channel = cast(dict[str, Any], payload.get("channel") or {})
    team_id, user_id, channel_id = (
        str(team.get("id") or ""),
        str(user.get("id") or ""),
        str(channel.get("id") or ""),
    )
    if not team_id or not user_id or not channel_id or channel_id.startswith("D"):
        return
    actions = cast(list[dict[str, Any]], payload.get("actions") or [])
    selected = cast(dict[str, Any], actions[0].get("selected_option") or {}) if actions else {}
    choice = decode_choice(selected.get("value"))
    if choice is None:
        return
    key, level, value = choice
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    actor = _actor(
        client,
        tenant_id=tenant_id,
        team_id=team_id,
        user_id=user_id,
        account_id=await _account_id(runtime, tenant_id=tenant_id, user_id=user_id),
    )
    try:
        change = await change_setting(
            runtime.sessionmaker,
            runtime.settings,
            actor,
            key=key,
            level=level,
            value=value,
            place=SettingsPlace.of("slack", channel_id, None),
        )
    except (ChannelChangeRefused, StaleChannelValue) as exc:
        await post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text=str(exc), error=True
        )
        return
    await post_ephemeral(client, channel_id=channel_id, user_id=user_id, text=change.note)


__all__ = [
    "ACTION_ID",
    "build_settings_blocks",
    "handle_settings_change",
    "handle_settings_command",
]
