"""Requests waiting under Slack's private GitHub screen."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Final, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_policy import gather_target_facts
from daimon.adapters.slack.agent_setup import panel_views
from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.agent_setup.github_requests import update_requester_card
from daimon.adapters.slack.agent_setup.read import load_panel_roster
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.channel_admin_groups import channel_admin_caller
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn_preserving_mentions
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.github_panel import connect_link, safe_github_error, sync_connect_admin
from daimon.core.github_request_cards import admin_card
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.operation_policy import decide_operation
from daimon.core.stores.github_access import list_authorized_repos
from daimon.core.stores.github_access_requests import (
    AccessRequest,
    cancel_request,
    dismiss_delivery,
    get_request,
    list_asker_requests,
    list_waiting,
    set_status,
)
from daimon.core.stores.github_links import account_link_status
from daimon.core.stores.github_personal_links import mint_link
from daimon.core.stores.github_request_actions import (
    approve_connected_request,
    approve_connection_request,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

ACTION_OPEN: Final = panel_views.ACTION_GITHUB_WAITING
ACTION_REVIEW: Final = "agent_setup__github_waiting_review"
ACTION_APPROVE: Final = "agent_setup__github_waiting_approve"
ACTION_CONNECT: Final = "agent_setup__github_waiting_connect"
ACTION_DECLINE: Final = "agent_setup__github_waiting_decline"
ACTION_HIDE: Final = "agent_setup__github_waiting_hide"
ACTION_CANCEL: Final = "agent_setup__github_waiting_cancel"
ACTION_BACK: Final = "agent_setup__github_waiting_back"
ACTION_PREVIOUS: Final = "agent_setup__github_waiting_previous"
ACTION_NEXT: Final = "agent_setup__github_waiting_next"
ACTIONS: Final = frozenset(
    {
        ACTION_OPEN,
        ACTION_REVIEW,
        ACTION_APPROVE,
        ACTION_CONNECT,
        ACTION_DECLINE,
        ACTION_HIDE,
        ACTION_CANCEL,
        ACTION_BACK,
        ACTION_PREVIOUS,
        ACTION_NEXT,
    }
)


async def _may_manage_request(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    request: AccessRequest,
    user_id: str,
    account_id: uuid.UUID,
    is_admin: bool,
) -> bool:
    if is_admin:
        return True
    live_agent = await find_agent_by_derived_uuid(
        runtime.anthropic, tenant_id=request.tenant_id, agent_id=request.agent_id
    )
    if live_agent is None or live_agent.id != request.ma_agent_id:
        return False
    caller = await channel_admin_caller(
        runtime, client, tenant_id=request.tenant_id, user_id=user_id, is_admin=False
    )
    facts = await gather_target_facts(
        runtime,
        operation="github_grant",
        tenant_id=request.tenant_id,
        agent_names=agent_pin_names(live_agent.name, live_agent.metadata),
        ma_agent_id=str(live_agent.id),
        is_daimon_managed=live_agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
        caller=caller,
        caller_account_id=account_id,
    )
    return decide_operation("github_grant", is_admin=False, target=facts) == "allow"


async def _can_see_request(client: AsyncWebClient, row: AccessRequest, user_id: str) -> bool:
    if row.parent_channel_id.startswith("D"):
        return True
    try:
        info = await client.conversations_info(  # pyright: ignore[reportUnknownMemberType]
            channel=row.parent_channel_id
        )
        raw_channel: object = info.get("channel")
        channel = cast("dict[str, object]", raw_channel) if isinstance(raw_channel, dict) else {}
        if not channel.get("is_private"):
            return True
        members = await client.conversations_members(  # pyright: ignore[reportUnknownMemberType]
            channel=row.parent_channel_id, limit=1000
        )
        return user_id in (members.get("members") or [])
    except SlackApiError:
        return False


def _section(text: str, *, action: str | None = None, value: str | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": escape_mrkdwn_preserving_mentions(text)},
    }
    if action is not None:
        block["accessory"] = _button(action, "Review", value)
    return block


def _button(action: str, label: str, value: str | None = None) -> dict[str, Any]:
    button: dict[str, Any] = {
        "type": "button",
        "action_id": action,
        "text": {"type": "plain_text", "text": label},
    }
    if value is not None:
        button["value"] = value
    return button


def build_view(
    meta: PanelMetadata,
    *,
    requests: tuple[AccessRequest, ...],
    own: tuple[AccessRequest, ...],
    connected_names: frozenset[str],
    selected: AccessRequest | None = None,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if selected is None:
        if not requests:
            blocks.append(_section("Nothing waiting."))
        for row in requests[meta.page * 18 : (meta.page + 1) * 18]:
            where = (
                "in a direct message"
                if row.parent_channel_id.startswith("D")
                else f"in <#{row.parent_channel_id}>"
            )
            age = f"<!date^{int(row.created_at.timestamp())}^{{relative}}|earlier>"
            blocks.append(
                _section(
                    f"<@{row.requester_platform_user_id}>\n{row.agent_name}",
                    action=ACTION_REVIEW,
                    value=str(row.id),
                )
            )
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": where},
                        {"type": "mrkdwn", "text": age},
                    ],
                }
            )
        for row in own[meta.page * 8 : (meta.page + 1) * 8]:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": escape_mrkdwn_preserving_mentions(
                            f"Your request\n{row.agent_name}"
                        ),
                    },
                    "accessory": _button(ACTION_CANCEL, "Cancel request", str(row.id)),
                }
            )
        if len(requests) > 18 or len(own) > 8:
            nav: list[dict[str, Any]] = []
            if meta.page:
                nav.append(_button(ACTION_PREVIOUS, "Previous"))
            if (meta.page + 1) * 18 < len(requests) or (meta.page + 1) * 8 < len(own):
                nav.append(_button(ACTION_NEXT, "Next"))
            if nav:
                blocks.append({"type": "actions", "elements": nav})
    else:
        all_connected = all(name.casefold() in connected_names for name in selected.repo_names)
        card = admin_card(
            selected,
            connected_names=tuple(selected.repo_names) if all_connected else (),
            channel_label=(
                ""
                if selected.parent_channel_id.startswith("D")
                else f"<#{selected.parent_channel_id}>"
            ),
            requester_label=f"<@{selected.requester_platform_user_id}>",
            ability=selected.required_ability,
        )
        title, _, detail = card.text.partition("\n")
        blocks.append(_section(title))
        if detail:
            blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": detail}]})
        buttons: list[dict[str, Any]] = []
        if card.primary:
            buttons.append(
                _button(
                    ACTION_APPROVE
                    if card.primary in ("Add repo", "Add repos", "Allow")
                    else ACTION_CONNECT,
                    card.primary,
                    str(selected.id),
                )
            )
        for label in card.secondary:
            buttons.append(
                _button(
                    ACTION_DECLINE if label == "Decline" else ACTION_HIDE,
                    label,
                    str(selected.id),
                )
            )
        if buttons:
            blocks.append({"type": "divider"})
            blocks.append({"type": "actions", "elements": buttons})
    blocks.append({"type": "actions", "elements": [_button(ACTION_BACK, "◀ Back")]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "GitHub on Daimon"}]})
    return finish_modal(
        title="Requests waiting",
        blocks=blocks,
        private_metadata=encode_panel_metadata(meta.with_view("github_waiting")),
        callback_id="agent_setup__github_waiting",
    )


async def _load(
    runtime: SlackRuntime,
    *,
    client: AsyncWebClient,
    user_id: str,
    tenant_id: uuid.UUID,
    channel_id: str,
    account_id: uuid.UUID,
    is_admin: bool,
) -> tuple[tuple[AccessRequest, ...], tuple[AccessRequest, ...], frozenset[str]]:
    async with runtime.sessionmaker() as session:
        roster = await load_panel_roster(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            channel_id=channel_id or None,
            thread_id=None,
            default=runtime.deployment_default,
            is_admin=is_admin,
        )
        visible = frozenset(
            derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=row.ma_agent_id)
            for row in roster.rows
        )
        requests = await list_waiting(session, tenant_id=tenant_id, visible_agent_ids=visible)
        own = await list_asker_requests(session, tenant_id=tenant_id, account_id=account_id)
        repos = await list_authorized_repos(session, tenant_id=tenant_id)
    connected_names = frozenset(
        repo.repo_full_name.casefold() for repo in repos if repo.status == "active"
    )
    visible_requests: list[AccessRequest] = []
    for row in requests:
        if not await _can_see_request(client, row, user_id):
            continue
        if not is_admin:
            if row.parent_channel_id.startswith("D") or not all(
                name.casefold() in connected_names for name in row.repo_names
            ):
                continue
            if not await _may_manage_request(
                runtime,
                client,
                request=row,
                user_id=user_id,
                account_id=account_id,
                is_admin=False,
            ):
                continue
        visible_requests.append(row)
    return (
        tuple(visible_requests),
        tuple(own),
        connected_names,
    )


async def handle(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    payload: dict[str, Any],
    *,
    action: dict[str, Any],
    meta: PanelMetadata,
    team_id: str,
    user_id: str,
) -> bool:
    action_id = str(action.get("action_id") or "")
    if action_id not in ACTIONS:
        return False
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    is_admin = await resolve_is_admin(client, user_id=user_id)
    async with runtime.sessionmaker.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
    view_info = cast("dict[str, Any]", payload.get("view") or {})
    view_id = str(view_info.get("id") or "")
    trigger_id = str(payload.get("trigger_id") or "")
    request_id: uuid.UUID | None = None
    if action_id in (
        ACTION_REVIEW,
        ACTION_APPROVE,
        ACTION_CONNECT,
        ACTION_DECLINE,
        ACTION_HIDE,
        ACTION_CANCEL,
    ):
        try:
            request_id = uuid.UUID(str(action.get("value") or ""))
        except ValueError:
            return True
    if action_id == ACTION_CANCEL and request_id is not None:
        async with runtime.sessionmaker.begin() as session:
            await cancel_request(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                account_id=principal.account_id,
            )
    elif action_id in (ACTION_APPROVE, ACTION_CONNECT, ACTION_DECLINE, ACTION_HIDE):
        if request_id is None:
            return True
        async with runtime.sessionmaker() as session:
            request = await get_request(session, tenant_id=tenant_id, request_id=request_id)
        if request is None:
            return True
        if not await _can_see_request(client, request, user_id):
            return True
        if action_id != ACTION_HIDE and not is_admin:
            await post_ephemeral(
                client,
                channel_id=meta.channel_id or user_id,
                user_id=user_id,
                text="Only a workspace admin can approve GitHub requests.",
            )
            return True
        if not await _may_manage_request(
            runtime,
            client,
            request=request,
            user_id=user_id,
            account_id=principal.account_id,
            is_admin=is_admin,
        ):
            return True
        if not is_admin:
            async with runtime.sessionmaker() as session:
                connected = {
                    repo.repo_full_name.casefold()
                    for repo in await list_authorized_repos(session, tenant_id=tenant_id)
                    if repo.status == "active"
                }
            if not all(name.casefold() in connected for name in request.repo_names):
                return True
        try:
            if action_id == ACTION_APPROVE:
                async with runtime.sessionmaker.begin() as session:
                    changed = await approve_connected_request(
                        session,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_id=principal.account_id,
                    )
                if changed:
                    personal_url: str | None = None
                    if runtime.settings.mcp.app_root_url is not None:
                        async with runtime.sessionmaker() as session:
                            linked = await account_link_status(
                                session, account_id=request.requester_account_id
                            )
                        if not linked:
                            async with runtime.sessionmaker.begin() as session:
                                personal_url = await mint_link(
                                    session,
                                    tenant_id=tenant_id,
                                    account_id=request.requester_account_id,
                                    platform="slack",
                                    platform_user_id=request.requester_platform_user_id,
                                    root_url=str(runtime.settings.mcp.app_root_url),
                                )
                    await update_requester_card(
                        runtime,
                        client,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        text=(
                            "Link GitHub so Daimon can check that you have access to "
                            "what this request needs."
                            if personal_url is not None
                            else f"✓ Added. {request.agent_name} is continuing."
                        ),
                        can_cancel=personal_url is not None,
                        link_url=personal_url,
                    )
            elif action_id == ACTION_CONNECT:
                team = cast("dict[str, Any]", payload.get("team") or {})
                user = cast("dict[str, Any]", payload.get("user") or {})
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
                        preselected_repo_full_names=request.repo_names,
                    )
                    changed = await approve_connection_request(
                        session,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_id=principal.account_id,
                    )
                await send_link(
                    client, channel_id=meta.channel_id or user_id, user_id=user_id, url=url
                )
                if changed:
                    await update_requester_card(
                        runtime,
                        client,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        text="Waiting for GitHub confirmation.",
                        can_cancel=True,
                    )
            elif action_id == ACTION_DECLINE:
                async with runtime.sessionmaker.begin() as session:
                    changed = await set_status(
                        session,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        expected=request.status,
                        status="declined",
                    )
                if changed:
                    await update_requester_card(
                        runtime,
                        client,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        text="An admin declined GitHub access for this request.",
                        can_cancel=True,
                    )
            else:
                async with runtime.sessionmaker.begin() as session:
                    await dismiss_delivery(
                        session,
                        tenant_id=tenant_id,
                        request_id=request_id,
                        account_id=principal.account_id,
                    )
        except ValueError as error:
            await post_ephemeral(
                client,
                channel_id=meta.channel_id or user_id,
                user_id=user_id,
                text=safe_github_error(error),
            )
            return True
    if (
        action_id == ACTION_BACK
        and meta.view == "github_waiting"
        and meta.github_step != "request_review"
    ):
        from daimon.adapters.slack.agent_setup.actions import github_can_choose_agent
        from daimon.core.stores.github_connected_repos import summary

        async with runtime.sessionmaker() as session:
            home = await summary(session, tenant_id=tenant_id) if is_admin else None
            linked_login = await account_link_status(session, account_id=principal.account_id)
        view = panel_views.build_github_home_view(
            meta,
            connected_count=home.count if home else 0,
            is_admin=is_admin,
            owners=home.owners if home else (),
            agent_count=home.agent_count if home else 0,
            linked_login=linked_login,
            can_choose_agent=await github_can_choose_agent(
                runtime, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
            ),
        )
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id, view=view
        )
        return True
    requests, own, connected = await _load(
        runtime,
        client=client,
        user_id=user_id,
        tenant_id=tenant_id,
        channel_id=meta.channel_id,
        account_id=principal.account_id,
        is_admin=is_admin,
    )
    selected = (
        next((row for row in requests if row.id == request_id), None)
        if action_id == ACTION_REVIEW
        else None
    )
    view = build_view(
        dataclasses.replace(
            meta,
            view="github_waiting",
            github_step="request_review" if selected else "pick",
            page=(
                max(0, meta.page - 1)
                if action_id == ACTION_PREVIOUS
                else meta.page + 1
                if action_id == ACTION_NEXT
                else meta.page
            ),
        ),
        requests=requests,
        own=own,
        connected_names=connected,
        selected=selected,
    )
    if action_id == ACTION_OPEN:
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=trigger_id, view=view
        )
    else:
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id, view=view
        )
    return True
