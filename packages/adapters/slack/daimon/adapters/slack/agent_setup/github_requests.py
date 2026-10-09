"""Actions on ephemeral Slack GitHub request cards."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.github_card_ui import github_card_blocks
from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_connect_cards import (
    ConnectCard,
    connect_button_blocks,
    resolve_connect_card,
)
from daimon.core.github_panel import connect_link, safe_github_error, sync_connect_admin
from daimon.core.github_request_cards import slack_mrkdwn_escape
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_access import list_authorized_repos
from daimon.core.stores.github_access_requests import (
    cancel_request,
    dismiss_delivery,
    get_delivery,
    lock_delivery_slot,
    lookup_request,
    record_reposted_requester_card,
    set_status,
)
from daimon.core.stores.github_app_installations import get as get_app_installation
from daimon.core.stores.github_request_actions import (
    approve_connected_request,
    approve_connection_request,
)
from daimon.core.stores.identity import find_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


async def _replace_card(
    client: AsyncWebClient, *, channel_id: str, thread_id: str, user_id: str, text: str
) -> None:
    await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
        channel=channel_id,
        thread_ts=thread_id,
        user=user_id,
        text=text,
        blocks=github_card_blocks(text),
    )


def _review_modal(
    request_id: uuid.UUID,
    *,
    channel_id: str,
    message_id: str,
    agent_name: str,
    repo_names: list[str],
    other_repo_count: int,
    ability: str,
) -> dict[str, Any]:
    metadata = json.dumps(
        {"request_id": str(request_id), "channel_id": channel_id, "message_id": message_id}
    )
    repo_lines = [f"Repo: {slack_mrkdwn_escape(name)}" for name in repo_names]
    if other_repo_count:
        repo_lines.append(f"{other_repo_count} other repo(s) not connected yet")
    repo_detail = "\n".join(repo_lines)
    return {
        "type": "modal",
        "callback_id": "github_request_review",
        "title": {"type": "plain_text", "text": "GitHub access"},
        "close": {"type": "plain_text", "text": "Close"},
        "private_metadata": metadata,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{slack_mrkdwn_escape(agent_name)} needs GitHub access*\n"
                        f"{repo_detail}\n"
                        f"Access: {slack_mrkdwn_escape(ability)}"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "github_request__decision",
                        "value": f"{request_id}:approve",
                        "text": {"type": "plain_text", "text": "Approve"},
                    },
                    {
                        "type": "button",
                        "action_id": "github_request__decision",
                        "value": f"{request_id}:decline",
                        "text": {"type": "plain_text", "text": "Decline"},
                    },
                    {
                        "type": "button",
                        "action_id": "github_request__decision",
                        "value": f"{request_id}:connect",
                        "text": {"type": "plain_text", "text": "Connect and add"},
                    },
                ],
            },
        ],
    }


async def _replace_modal(
    client: AsyncWebClient,
    payload: dict[str, Any],
    text: str,
    *,
    url: str | None = None,
    card: ConnectCard | None = None,
) -> None:
    view = cast("dict[str, Any]", payload.get("view") or {})
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    if url:
        connect_blocks = connect_button_blocks(url, card=card)
        actions = next(block for block in connect_blocks if block["type"] == "actions")
        actions["elements"][0]["action_id"] = "github_request__link"
        blocks = connect_blocks
    await client.views_update(  # pyright: ignore[reportUnknownMemberType]
        view_id=str(view.get("id") or ""),
        view={
            "type": "modal",
            "callback_id": "github_request_review",
            "title": {"type": "plain_text", "text": "GitHub access"},
            "close": {"type": "plain_text", "text": "Close"},
            "private_metadata": str(view.get("private_metadata") or ""),
            "blocks": blocks,
        },
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
    """Post an updated ephemeral requester card at the original conversation."""
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
    if delivery is None:
        return
    try:
        buttons: list[dict[str, Any]] = []
        if can_cancel or link_url:
            if link_url:
                buttons.append(
                    {
                        "type": "button",
                        "action_id": "github_personal__open",
                        "url": link_url,
                        "text": {"type": "plain_text", "text": "Get a new link"},
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
        async with runtime.sessionmaker.begin() as session:
            await lock_delivery_slot(
                session, request_id=request_id, recipient_account_id=request.requester_account_id
            )
            sent = await client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=request.parent_channel_id,
                thread_ts=request.thread_id,
                user=request.requester_platform_user_id,
                text=text,
                blocks=blocks,
            )
            message_id = sent.get("message_ts") or sent.get("ts")
            if isinstance(message_id, str) and message_id:
                await record_reposted_requester_card(
                    session,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    recipient_account_id=request.requester_account_id,
                    message_id=message_id,
                )
    except SlackApiError:
        return


async def handle_action(runtime: SlackRuntime, payload: dict[str, Any]) -> None:
    team = cast("dict[str, Any]", payload.get("team") or {})
    user = cast("dict[str, Any]", payload.get("user") or {})
    channel = cast("dict[str, Any]", payload.get("channel") or {})
    message = cast("dict[str, Any]", payload.get("message") or {})
    container = cast("dict[str, Any]", payload.get("container") or {})
    action = cast("dict[str, Any]", (payload.get("actions") or [{}])[0])
    team_id = str(team.get("id") or "")
    user_id = str(user.get("id") or "")
    channel_id = str(channel.get("id") or "")
    message_id = str(container.get("message_ts") or message.get("ts") or "")
    action_id = str(action.get("action_id") or "")
    modal_view = cast("dict[str, Any]", payload.get("view") or {})
    modal = bool(modal_view)
    if modal:
        try:
            metadata = json.loads(str(modal_view.get("private_metadata") or ""))
        except (ValueError, TypeError):
            return
        if not isinstance(metadata, dict):
            return
        metadata = cast("dict[str, object]", metadata)
        channel_id = str(metadata.get("channel_id") or "")
        message_id = str(metadata.get("message_id") or "")
    value = str(action.get("value") or "")
    if action_id == "github_request__review":
        parts = [value, "review"]
    else:
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
    decision = parts[1]
    shared = decision == "review" or modal
    if (
        request is None
        or request.tenant_id != tenant_id
        or request.parent_channel_id != channel_id
        or (
            request.admin_card_message_id != message_id
            if shared
            else principal is None
            or delivery is None
            or delivery.dismissed_at is not None
            or (delivery.message_id is not None and delivery.message_id != message_id)
        )
    ):
        if modal:
            await _replace_modal(client, payload, "This request is unavailable.")
        else:
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text="This request is unavailable.",
            )
        return
    if decision == "review":
        if request.status not in ("open", "waiting_github"):
            await post_ephemeral(
                client, channel_id=channel_id, user_id=user_id, text="This request is unavailable."
            )
            return
        if not await resolve_is_admin(client, user_id=user_id):
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text="Only a workspace admin can review GitHub requests.",
            )
            return
        async with runtime.sessionmaker() as session:
            connected: dict[str, str] = {}
            for repo in await list_authorized_repos(session, tenant_id=tenant_id):
                if repo.status != "active":
                    continue
                installation = await get_app_installation(
                    session, installation_id=repo.installation_id
                )
                if installation and repo.repo_full_name in installation.repo_full_names:
                    connected[repo.repo_full_name.casefold()] = repo.repo_full_name
        connected_names = [
            connected[name.casefold()]
            for name in request.repo_names
            if name.casefold() in connected
        ]
        level = "Read only" if request.required_ability == "read" else "Read and write"
        await client.views_open(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=str(payload.get("trigger_id") or ""),
            view=_review_modal(
                request_id,
                channel_id=channel_id,
                message_id=message_id,
                agent_name=request.agent_name,
                repo_names=connected_names,
                other_repo_count=len(request.repo_names) - len(connected_names),
                ability=level,
            ),
        )
        return
    if principal is None and shared:
        if not await resolve_is_admin(client, user_id=user_id):
            await _replace_modal(
                client, payload, "Only a workspace admin can approve GitHub requests."
            )
            return
        async with runtime.sessionmaker.begin() as session:
            await sync_connect_admin(
                session,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                verified_tenant_admin=True,
            )
            principal = await find_platform_principal(
                session, tenant_id=tenant_id, platform="slack", external_id=user_id
            )
    if principal is None:
        return

    async def respond(text: str, *, url: str | None = None) -> None:
        if modal:
            card = (
                await resolve_connect_card(
                    runtime.sessionmaker,
                    runtime.settings,
                    tenant_id=tenant_id,
                    platform="slack",
                    workspace_id=team_id,
                    agent_name=request.agent_name,
                )
                if url
                else None
            )
            await _replace_modal(client, payload, text, url=url, card=card)
        else:
            await _replace_card(
                client,
                channel_id=channel_id,
                thread_id=request.thread_id,
                user_id=user_id,
                text=text,
            )

    if decision == "cancel":
        async with runtime.sessionmaker.begin() as session:
            changed = await cancel_request(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
        await respond("Request cancelled." if changed else "This request is unavailable.")
        return
    if decision == "hide":
        async with runtime.sessionmaker.begin() as session:
            changed = await dismiss_delivery(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
        await respond("Hidden for you." if changed else "This request is unavailable.")
        return
    is_admin = await resolve_is_admin(client, user_id=user_id)
    if not is_admin:
        await respond("Only a workspace admin can approve GitHub requests.")
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
        await respond("Declined." if changed else "This request is unavailable.")
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
            await respond(safe_github_error(error))
            return
        await respond(
            f"✓ Added. {request.agent_name} is continuing."
            if changed
            else "This request is unavailable."
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
            await sync_connect_admin(
                session,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                verified_tenant_admin=is_admin,
            )
            url = await connect_link(
                session,
                settings=runtime.settings,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                verified_tenant_admin=is_admin,
                requester_label=str(user.get("name") or user_id),
                workspace_label=str(team.get("name") or team_id),
                origin_parent_channel_id=request.parent_channel_id,
                origin_thread_id=request.thread_id,
                origin_followup_token=str(payload.get("response_url") or "") or None,
                origin_followup_expires_at=(
                    datetime.now(UTC) + timedelta(minutes=30)
                    if payload.get("response_url")
                    else None
                ),
            )
            await approve_connection_request(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
    except ValueError as error:
        await respond(safe_github_error(error))
        return
    await respond("Waiting for GitHub confirmation.", url=url if modal else None)
    if not modal:
        await send_link(
            client, channel_id=channel_id, thread_id=request.thread_id, user_id=user_id, url=url
        )
    await update_requester_card(
        runtime,
        client,
        tenant_id=tenant_id,
        request_id=request_id,
        text="Waiting for GitHub confirmation.",
        can_cancel=True,
        skip_account_id=principal.account_id,
    )
