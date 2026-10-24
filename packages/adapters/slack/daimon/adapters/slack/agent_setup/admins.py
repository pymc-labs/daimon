"""Workspace admin grants, with live platform checks on every grant/revoke click."""

from __future__ import annotations

from typing import Any

from daimon.adapters.slack.admin import resolve_is_platform_admin
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.daimon_admins import change_grant, list_admins
from slack_sdk.web.async_client import AsyncWebClient

ACTION_OPEN = "agent_setup__admins:open"
ACTION_GRANT = "agent_setup__admins:grant"
ACTION_REVOKE = "agent_setup__admins:revoke"
ACTIONS = frozenset({ACTION_OPEN, ACTION_GRANT, ACTION_REVOKE})


async def admins_view(
    runtime: SlackRuntime, client: AsyncWebClient, *, meta: PanelMetadata, user_id: str
) -> dict[str, Any]:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=meta.team_id)
    async with runtime.sessionmaker() as session:
        grants = await list_admins(session, tenant_id=tenant_id)
    listing = (
        "\n".join(
            f"<@{g.subject_id}> · by {g.granted_by or 'erased'} · {g.granted_at:%Y-%m-%d}"
            for g in grants[:30]
        )
        or "No explicit grants."
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Platform admins always have access. "
                    "Only platform admins may grant or revoke. "
                    "Billing and caps stay platform-only. "
                    "Granted admins can change what agents do, "
                    "including agents platform admins use."
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": listing}},
    ]
    if len(grants) > 30:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": "More grants: use `daimon admins list`.",
                },
            }
        )
    if await resolve_is_platform_admin(client, user_id=user_id):
        blocks.extend(
            [
                {
                    "type": "input",
                    "block_id": "admin_subject",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "User"},
                    "element": {"type": "users_select", "action_id": "user"},
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": ACTION_GRANT,
                            "text": {"type": "plain_text", "text": "Grant"},
                        },
                        {
                            "type": "button",
                            "action_id": ACTION_REVOKE,
                            "style": "danger",
                            "text": {"type": "plain_text", "text": "Revoke"},
                        },
                    ],
                },
            ]
        )
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Daimon admins"},
        "close": {"type": "plain_text", "text": "Close"},
        "private_metadata": encode_panel_metadata(meta.with_view("admins")),
        "blocks": blocks,
    }


async def handle_admin_action(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    payload: dict[str, Any],
    meta: PanelMetadata,
    user_id: str,
    team_id: str,
    action_id: str,
) -> None:
    # Tenant authority comes from the signed envelope, never the rendered modal.
    if not team_id or meta.team_id != team_id:
        return
    view: dict[str, Any] = payload.get("view") or {}
    if action_id != ACTION_OPEN:
        if not await resolve_is_platform_admin(client, user_id=user_id):
            await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=meta.channel_id or user_id,
                user=user_id,
                text="Only platform admins may grant or revoke Daimon admins.",
            )  # pyright: ignore[reportUnknownMemberType]
            return
        state: dict[str, Any] = view.get("state") or {}
        values: dict[str, Any] = state.get("values") or {}
        block: dict[str, Any] = values.get("admin_subject") or {}
        element: dict[str, Any] = block.get("user") or {}
        subject = str(element.get("selected_user") or "")
        if not subject:
            return
        async with runtime.sessionmaker.begin() as session:
            await change_grant(
                session,
                tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
                platform="slack",
                subject_kind="user",
                subject_id=subject,
                actor_user_id=user_id,
                is_platform_admin=True,
                revoke=action_id == ACTION_REVOKE,
            )
    rendered = await admins_view(runtime, client, meta=meta, user_id=user_id)
    if action_id == ACTION_OPEN:
        await client.views_push(trigger_id=str(payload.get("trigger_id") or ""), view=rendered)  # pyright: ignore[reportUnknownMemberType]
    else:
        await client.views_update(view_id=str(view.get("id") or ""), view=rendered)  # pyright: ignore[reportUnknownMemberType]
