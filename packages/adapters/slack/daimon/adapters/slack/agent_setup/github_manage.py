"""Server-wide connected GitHub repo settings in Slack."""

from __future__ import annotations

import dataclasses
from typing import Any, Final, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup import panel_views
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.modal_limits import finish_modal
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import safe_github_error
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.github_access import AuthorizedRepo, list_authorized_repos
from daimon.core.stores.github_connected_repos import (
    disconnect_github,
    disconnect_repo,
    set_repo_ability,
    summary,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

ACTION_OPEN: Final = "agent_setup__github_manage"
ACTION_CHANGE: Final = "agent_setup__github_manage_change"
ACTION_DISCONNECT: Final = "agent_setup__github_manage_disconnect"
ACTION_READ: Final = "agent_setup__github_manage_read"
ACTION_WRITE: Final = "agent_setup__github_manage_write"
ACTION_CONFIRM: Final = "agent_setup__github_manage_confirm"
ACTION_BACK: Final = "agent_setup__github_manage_back"
ACTION_DISCONNECT_ALL: Final = "agent_setup__github_manage_disconnect_all"
ACTION_CONFIRM_ALL: Final = "agent_setup__github_manage_confirm_all"
ACTION_SETTINGS_LINK: Final = "agent_setup__github_settings_link"
ACTION_PREVIOUS: Final = "agent_setup__github_manage_previous"
ACTION_NEXT: Final = "agent_setup__github_manage_next"
ACTIONS: Final = frozenset(
    {
        ACTION_OPEN,
        ACTION_CHANGE,
        ACTION_DISCONNECT,
        ACTION_READ,
        ACTION_WRITE,
        ACTION_CONFIRM,
        ACTION_BACK,
        ACTION_DISCONNECT_ALL,
        ACTION_CONFIRM_ALL,
        ACTION_SETTINGS_LINK,
        ACTION_PREVIOUS,
        ACTION_NEXT,
    }
)


def _button(action: str, label: str, value: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "button",
        "action_id": action,
        "text": {"type": "plain_text", "text": label},
    }
    if value is not None:
        item["value"] = value
    return item


def build_view(meta: PanelMetadata, repos: list[AuthorizedRepo]) -> dict[str, Any]:
    selected = next((r for r in repos if r.repo_id == meta.repo_id), None)
    blocks: list[dict[str, Any]] = []
    if meta.github_step == "manage_disconnect_all":
        blocks.extend(
            [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": escape_mrkdwn("Disconnect GitHub from this workspace?"),
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": escape_mrkdwn(
                                "Agents lose access to these repos right away. "
                                "Waiting requests are cancelled."
                            ),
                        },
                        {
                            "type": "mrkdwn",
                            "text": escape_mrkdwn(
                                "Chats, and changes already made on GitHub, stay."
                            ),
                        },
                    ],
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "To remove Daimon from GitHub too:",
                    },
                },
            ]
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open GitHub settings ↗"},
                        "url": "https://github.com/settings/installations",
                        "action_id": ACTION_SETTINGS_LINK,
                    },
                    _button(ACTION_CONFIRM_ALL, "Disconnect"),
                    _button(ACTION_BACK, "◀ Back"),
                ],
            }
        )
    elif meta.github_step == "manage_change" and selected is not None:
        ability = "Read only" if selected.max_access == "read" else "Read and write"
        blocks.extend(
            [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": escape_mrkdwn(selected.repo_full_name)},
                },
                {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*Can*\n{ability}"}]},
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                "*Read and write*\nPush branches, open issues and pull requests."
                            ),
                        },
                        {
                            "type": "mrkdwn",
                            "text": "*Read only*\nRead code, issues and pull requests.",
                        },
                    ],
                },
                {"type": "divider"},
            ]
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_WRITE, "Read and write"),
                    _button(ACTION_READ, "Read only"),
                    _button(ACTION_BACK, "◀ Back"),
                ],
            }
        )
    elif meta.github_step == "manage_disconnect" and selected is not None:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": escape_mrkdwn(
                        f"Disconnect {selected.repo_full_name}? Agents using it lose it."
                    ),
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_CONFIRM, "Disconnect"),
                    _button(ACTION_BACK, "◀ Back"),
                ],
            }
        )
    else:
        for repo in repos[meta.page * 20 : (meta.page + 1) * 20]:
            ability = "Read only" if repo.max_access == "read" else "Read and write"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": escape_mrkdwn(f"{repo.repo_full_name} — {ability}"),
                    },
                }
            )
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        _button(ACTION_CHANGE, "Change", str(repo.repo_id)),
                        _button(ACTION_DISCONNECT, "Disconnect", str(repo.repo_id)),
                    ],
                }
            )
        nav: list[dict[str, Any]] = []
        if meta.page:
            nav.append(_button(ACTION_PREVIOUS, "Previous"))
        if (meta.page + 1) * 20 < len(repos):
            nav.append(_button(ACTION_NEXT, "Next"))
        if nav:
            blocks.append({"type": "actions", "elements": nav})
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    _button(ACTION_DISCONNECT_ALL, "Disconnect GitHub"),
                    _button(ACTION_BACK, "◀ Back"),
                ],
            }
        )
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "GitHub on Daimon"}]})
    return finish_modal(
        title="Connected repos",
        blocks=blocks,
        private_metadata=encode_panel_metadata(meta.with_view("github_manage")),
        callback_id="agent_setup__github_manage",
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
    if action_id == ACTION_SETTINGS_LINK:
        return True
    if not await resolve_is_admin(client, user_id=user_id):
        return True
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    view_info = cast("dict[str, Any]", payload.get("view") or {})
    if action_id == ACTION_BACK and meta.github_step == "pick":
        async with runtime.sessionmaker() as session:
            home = await summary(session, tenant_id=tenant_id)
        view = panel_views.build_github_home_view(
            meta,
            connected_count=home.count,
            owners=home.owners,
            agent_count=home.agent_count,
        )
    else:
        selected_id = meta.repo_id
        step = meta.github_step
        if action_id in (ACTION_CHANGE, ACTION_DISCONNECT):
            try:
                selected_id = int(str(action.get("value") or ""))
            except ValueError:
                return True
            step = "manage_change" if action_id == ACTION_CHANGE else "manage_disconnect"
        elif action_id == ACTION_DISCONNECT_ALL:
            step = "manage_disconnect_all"
        elif action_id == ACTION_BACK or action_id in (ACTION_PREVIOUS, ACTION_NEXT):
            step = "pick"
        if action_id in (ACTION_READ, ACTION_WRITE, ACTION_CONFIRM, ACTION_CONFIRM_ALL):
            if action_id == ACTION_CONFIRM_ALL and step != "manage_disconnect_all":
                return True
            if action_id == ACTION_CONFIRM_ALL:
                async with runtime.sessionmaker.begin() as session:
                    principal = await get_or_create_platform_principal(
                        session,
                        tenant_id=tenant_id,
                        platform="slack",
                        external_id=user_id,
                    )
                    account = await get_account(session, principal.account_id)
                    if account is None or account.is_external:
                        return True
                    await disconnect_github(session, tenant_id=tenant_id, account_id=account.id)
                step = "pick"
                selected_id = None
                async with runtime.sessionmaker() as session:
                    home = await summary(session, tenant_id=tenant_id)
                view = panel_views.build_github_home_view(
                    meta,
                    connected_count=home.count,
                    owners=home.owners,
                    agent_count=home.agent_count,
                )
                await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                    view_id=str(view_info.get("id") or ""), view=view
                )
                return True
            if selected_id is None or step not in ("manage_change", "manage_disconnect"):
                return True
            try:
                async with runtime.sessionmaker.begin() as session:
                    principal = await get_or_create_platform_principal(
                        session,
                        tenant_id=tenant_id,
                        platform="slack",
                        external_id=user_id,
                    )
                    account = await get_account(session, principal.account_id)
                    if account is None or account.is_external:
                        return True
                    if action_id == ACTION_CONFIRM and step == "manage_disconnect":
                        await disconnect_repo(
                            session,
                            tenant_id=tenant_id,
                            repo_id=selected_id,
                            account_id=account.id,
                        )
                    elif action_id in (ACTION_READ, ACTION_WRITE) and step == "manage_change":
                        await set_repo_ability(
                            session,
                            tenant_id=tenant_id,
                            repo_id=selected_id,
                            ability="read" if action_id == ACTION_READ else "write",
                            account_id=account.id,
                        )
                    else:
                        return True
            except ValueError as error:
                await post_ephemeral(
                    client,
                    channel_id=meta.channel_id or user_id,
                    user_id=user_id,
                    text=safe_github_error(error),
                )
                return True
            step = "pick"
        async with runtime.sessionmaker() as session:
            repos = [
                r
                for r in await list_authorized_repos(session, tenant_id=tenant_id)
                if r.status == "active"
            ]
        page = (
            max(0, meta.page - 1)
            if action_id == ACTION_PREVIOUS
            else meta.page + 1
            if action_id == ACTION_NEXT
            else meta.page
        )
        next_meta = dataclasses.replace(meta, github_step=step, repo_id=selected_id, page=page)
        view = build_view(next_meta, repos)
    if action_id == ACTION_OPEN:
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=str(payload.get("trigger_id") or ""),
            view=view,
        )
    else:
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=str(view_info.get("id") or ""),
            view=view,
        )
    return True
