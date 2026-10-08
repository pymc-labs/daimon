"""Actions on private Slack GitHub request cards."""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.github_card_ui import github_card_blocks
from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import connect_link, safe_github_error
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_access_requests import (
    cancel_request,
    dismiss_delivery,
    get_delivery,
    lookup_request,
    set_status,
)
from daimon.core.stores.github_request_actions import (
    approve_connected_request,
    approve_connection_request,
)
from daimon.core.stores.identity import find_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


async def _replace_card(
    client: AsyncWebClient, *, channel_id: str, message_id: str, text: str
) -> None:
    await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id,
        ts=message_id,
        text=text,
        blocks=github_card_blocks(text),
    )


async def update_requester_card(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    text: str,
    can_cancel: bool,
    link_url: str | None = None,
    skip_account_id: uuid.UUID | None = None,
) -> None:
    """Edit the private requester card after an admin decision."""
    async with runtime.sessionmaker() as session:
        request = await lookup_request(session, request_id=request_id)
        if request is None or request.tenant_id != tenant_id:
            return
        if request.requester_account_id == skip_account_id:
            return
        delivery = await get_delivery(
            session,
            tenant_id=tenant_id,
            request_id=request_id,
            recipient_account_id=request.requester_account_id,
        )
    if delivery is None or delivery.message_id is None:
        return
    try:
        opened = await client.conversations_open(  # pyright: ignore[reportUnknownMemberType]
            users=request.requester_platform_user_id
        )
        raw_channel: object = opened.get("channel")
        dm_channel = (
            str(cast("dict[str, object]", raw_channel).get("id") or "")
            if isinstance(raw_channel, dict)
            else ""
        )
        if not dm_channel:
            return
        buttons: list[dict[str, Any]] = []
        if can_cancel or link_url:
            if link_url:
                buttons.append(
                    {
                        "type": "button",
                        "action_id": "github_personal__open",
                        "url": link_url,
                        "text": {"type": "plain_text", "text": "Link GitHub"},
                    }
                )
            if can_cancel:
                buttons.append(
                    {
                        "type": "button",
                        "action_id": "github_request__decision",
                        "value": f"{request_id}:cancel",
                        "text": {"type": "plain_text", "text": "Cancel request"},
                    }
                )
        blocks = github_card_blocks(text, buttons=buttons)
        await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
            channel=dm_channel,
            ts=delivery.message_id,
            text=text,
            blocks=blocks,
        )
    except SlackApiError:
        return


async def handle_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    team = cast("dict[str, Any]", payload.get("team") or {})
    user = cast("dict[str, Any]", payload.get("user") or {})
    channel = cast("dict[str, Any]", payload.get("channel") or {})
    message = cast("dict[str, Any]", payload.get("message") or {})
    action = cast("dict[str, Any]", (payload.get("actions") or [{}])[0])
    team_id = str(team.get("id") or "")
    user_id = str(user.get("id") or "")
    channel_id = str(channel.get("id") or "")
    message_id = str(message.get("ts") or "")
    value = str(action.get("value") or "")
    parts = value.split(":")
    if len(parts) != 2 or parts[1] not in {"approve", "connect", "decline", "hide", "cancel"}:
        return
    try:
        request_id = uuid.UUID(parts[0])
    except ValueError:
        return
    if not team_id or not user_id or not channel_id or not message_id:
        return
    client = await resolve_web_client(runtime, team_id=team_id)
    if client is None:
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker() as session:
        request = await lookup_request(session, request_id=request_id)
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        delivery = (
            await get_delivery(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                recipient_account_id=principal.account_id,
            )
            if principal is not None
            else None
        )
    if (
        request is None
        or request.tenant_id != tenant_id
        or principal is None
        or delivery is None
        or delivery.dismissed_at is not None
        or delivery.message_id != message_id
    ):
        await post_ephemeral(
            client, channel_id=channel_id, user_id=user_id, text="This request is unavailable."
        )
        return
    decision = parts[1]
    if decision == "cancel":
        async with runtime.sessionmaker.begin() as session:
            changed = await cancel_request(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
        await _replace_card(
            client,
            channel_id=channel_id,
            message_id=message_id,
            text="Request cancelled." if changed else "This request is unavailable.",
        )
        return
    if decision == "hide":
        async with runtime.sessionmaker.begin() as session:
            changed = await dismiss_delivery(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
        await _replace_card(
            client,
            channel_id=channel_id,
            message_id=message_id,
            text="Hidden for you." if changed else "This request is unavailable.",
        )
        return
    is_admin = await resolve_is_admin(client, user_id=user_id)
    if not is_admin:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text="Only a workspace admin can approve GitHub requests.",
        )
        return
    if decision == "decline":
        async with runtime.sessionmaker.begin() as session:
            changed = await set_status(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                expected=request.status,
                status="declined",
            )
        await _replace_card(
            client,
            channel_id=channel_id,
            message_id=message_id,
            text="Declined." if changed else "This request is unavailable.",
        )
        if changed:
            await update_requester_card(
                runtime,
                client,
                tenant_id=tenant_id,
                request_id=request_id,
                text="An admin declined GitHub access for this request.",
                can_cancel=True,
                skip_account_id=principal.account_id,
            )
        return
    if decision == "approve":
        try:
            async with runtime.sessionmaker.begin() as session:
                changed = await approve_connected_request(
                    session,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    account_id=principal.account_id,
                )
        except ValueError as error:
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text=safe_github_error(error),
            )
            return
        await _replace_card(
            client,
            channel_id=channel_id,
            message_id=message_id,
            text=(
                f"✓ Added. {request.agent_name} is continuing."
                if changed
                else "This request is unavailable."
            ),
        )
        if changed:
            await update_requester_card(
                runtime,
                client,
                tenant_id=tenant_id,
                request_id=request_id,
                text=f"✓ Added. {request.agent_name} is continuing.",
                can_cancel=False,
                skip_account_id=principal.account_id,
            )
        return
    try:
        async with runtime.sessionmaker.begin() as session:
            url = await connect_link(
                session,
                settings=runtime.settings,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                verified_tenant_admin=is_admin,
                requester_label=str(user.get("name") or user_id),
                workspace_label=str(team.get("name") or team_id),
                preselected_repo_full_names=request.repo_names,
            )
            await approve_connection_request(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
    except ValueError as error:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=safe_github_error(error),
        )
        return
    await _replace_card(
        client,
        channel_id=channel_id,
        message_id=message_id,
        text="Waiting for GitHub confirmation.",
    )
    await send_link(client, channel_id=channel_id, user_id=user_id, url=url)
    await update_requester_card(
        runtime,
        client,
        tenant_id=tenant_id,
        request_id=request_id,
        text="Waiting for GitHub confirmation.",
        can_cancel=True,
        skip_account_id=principal.account_id,
    )
