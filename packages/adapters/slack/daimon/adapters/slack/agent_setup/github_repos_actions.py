"""Slack click handlers for GitHub repo grants."""

from __future__ import annotations

import dataclasses
from typing import Any, Literal, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_policy import refuse_unless_allowed_for_agent_name
from daimon.adapters.slack.agent_setup import github_repos
from daimon.adapters.slack.agent_setup.read import load_panel_roster
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import (
    activate_grants,
    load_grants_panel,
    remove_panel_grant,
    stage_panel_grant,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.github_access import deactivate_agent
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

_ACTIONS = frozenset(
    {
        github_repos.ACTION_OPEN,
        github_repos.ACTION_SELECT,
        github_repos.ACTION_STAGE,
        github_repos.ACTION_REMOVE,
        github_repos.ACTION_SWITCH,
        github_repos.ACTION_ACTIVATE,
        github_repos.ACTION_DEACTIVATE,
        github_repos.ACTION_PREVIOUS,
        github_repos.ACTION_NEXT,
        github_repos.ACTION_CONFIRM_REMOVE,
        github_repos.ACTION_CONFIRM_TURN_OFF,
        github_repos.ACTION_CONFIRM_UPDATE_KEY,
        github_repos.ACTION_CANCEL_CONFIRM,
    }
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
    """Handle a GitHub modal action; return False for other panel actions."""
    action_id = str(action.get("action_id") or "")
    if action_id not in _ACTIONS:
        return False
    channel_id = meta.channel_id or user_id
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    is_admin = await resolve_is_admin(client, user_id=user_id)
    name = (
        str(action.get("value") or "")
        if action_id == github_repos.ACTION_OPEN
        else meta.agent_name or ""
    )
    if not name:
        return True
    async with runtime.sessionmaker() as session:
        roster = await load_panel_roster(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            channel_id=meta.channel_id,
            thread_id=None,
            default=runtime.deployment_default,
            is_admin=is_admin,
        )
    agent = next((row for row in roster.rows if row.name == name), None)
    if agent is None:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text="That agent is no longer available here.",
        )
        return True
    refused = await refuse_unless_allowed_for_agent_name(
        runtime,
        client,
        operation="github_grant",
        tenant_id=tenant_id,
        agent_name=name,
        channel_id=channel_id,
        user_id=user_id,
    )
    if refused:
        return True
    agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id)
    selected_repo_id = meta.repo_id
    if action_id == github_repos.ACTION_SELECT:
        option = cast("dict[str, Any]", action.get("selected_option") or {})
        try:
            selected_repo_id = int(str(option.get("value") or ""))
        except (TypeError, ValueError):
            return True
    confirmation: str | None = None
    if action_id not in (
        github_repos.ACTION_OPEN,
        github_repos.ACTION_SELECT,
        github_repos.ACTION_PREVIOUS,
        github_repos.ACTION_NEXT,
    ):
        removed_pat = False
        try:
            async with runtime.sessionmaker.begin() as session:
                principal = await get_or_create_platform_principal(
                    session, tenant_id=tenant_id, platform="slack", external_id=user_id
                )
                account = await get_account(session, principal.account_id)
                if account is None or account.is_external:
                    raise ValueError("External participants cannot change GitHub repos.")
                actor = account.id
                panel = await load_grants_panel(session, tenant_id=tenant_id, agent_id=agent_id)
                repo = next((r for r in panel.repos if r.repo_id == selected_repo_id), None)
                if action_id == github_repos.ACTION_STAGE:
                    if repo is None:
                        raise ValueError("Choose a connected repo first.")
                    field, level = str(action.get("value") or "").split(":", 1)
                    if field not in ("baseline", "ceiling") or level not in (
                        "none",
                        "read",
                        "write",
                    ):
                        raise ValueError("That access choice is unavailable.")
                    baseline = level if field == "baseline" else (repo.baseline or "none")
                    ceiling = level if field == "ceiling" else (repo.ceiling or repo.max_access)
                    if field == "baseline" and baseline == "write" and ceiling == "read":
                        ceiling = "write"
                    if field == "ceiling" and baseline == "write" and ceiling == "read":
                        baseline = "read"
                    await stage_panel_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=repo.repo_id,
                        baseline_access=baseline,
                        ceiling_access=cast(Literal["read", "write"], ceiling),
                        account_id=actor,
                        is_working_repo=repo.working
                        or (
                            panel.working_repo is not None
                            and repo.full_name.casefold() == panel.working_repo.casefold()
                        ),
                    )
                elif action_id == github_repos.ACTION_REMOVE:
                    if repo is None:
                        raise ValueError("Choose a connected repo first.")
                    confirmation = "remove"
                elif action_id == github_repos.ACTION_CONFIRM_REMOVE:
                    if repo is None:
                        raise ValueError("Choose a connected repo first.")
                    await remove_panel_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=repo.repo_id,
                        account_id=actor,
                    )
                    if panel.mode == "app":
                        await activate_grants(
                            session,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            account_id=actor,
                        )
                elif action_id == github_repos.ACTION_SWITCH:
                    working = next(
                        (
                            r
                            for r in panel.repos
                            if panel.working_repo
                            and r.full_name.casefold() == panel.working_repo.casefold()
                        ),
                        None,
                    )
                    if working is None:
                        raise ValueError("Connect the working repo first.")
                    if working.max_access != "write":
                        raise ValueError("The working repo needs write access.")
                    await stage_panel_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=working.repo_id,
                        baseline_access="write",
                        ceiling_access="write",
                        account_id=actor,
                        is_working_repo=True,
                    )
                elif action_id == github_repos.ACTION_ACTIVATE:
                    if panel.mode == "legacy" and panel.has_pat:
                        confirmation = "update_key"
                    else:
                        removed_pat = await activate_grants(
                            session, tenant_id=tenant_id, agent_id=agent_id, account_id=actor
                        )
                elif action_id == github_repos.ACTION_DEACTIVATE:
                    confirmation = "turn_off"
                elif action_id == github_repos.ACTION_CONFIRM_TURN_OFF:
                    await deactivate_agent(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        changed_by_account_id=actor,
                    )
                elif action_id == github_repos.ACTION_CONFIRM_UPDATE_KEY:
                    removed_pat = await activate_grants(
                        session, tenant_id=tenant_id, agent_id=agent_id, account_id=actor
                    )
        except ValueError as error:
            await post_ephemeral(client, channel_id=channel_id, user_id=user_id, text=str(error))
            return True
        if removed_pat:
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text=f"Updated {name}. Open chats restart on the next turn.",
            )
    async with runtime.sessionmaker() as session:
        panel = await load_grants_panel(session, tenant_id=tenant_id, agent_id=agent_id)
    next_page = meta.page
    if action_id == github_repos.ACTION_PREVIOUS:
        next_page = max(0, next_page - 1)
        selected_repo_id = None
    elif action_id == github_repos.ACTION_NEXT:
        next_page += 1
        selected_repo_id = None
    elif action_id == github_repos.ACTION_OPEN:
        next_page = 0
    next_meta = dataclasses.replace(
        meta, view="github_repos", agent_name=name, repo_id=selected_repo_id, page=next_page
    )
    view = (
        github_repos.build_confirm_view(next_meta, panel, choice=confirmation)
        if action_id
        not in (
            github_repos.ACTION_OPEN,
            github_repos.ACTION_SELECT,
            github_repos.ACTION_PREVIOUS,
            github_repos.ACTION_NEXT,
        )
        and confirmation is not None
        else github_repos.build_view(next_meta, panel)
    )
    if action_id == github_repos.ACTION_OPEN:
        await client.views_push(trigger_id=str(payload.get("trigger_id") or ""), view=view)  # pyright: ignore[reportUnknownMemberType]
    else:
        view_info = cast("dict[str, Any]", payload.get("view") or {})
        view_id = str(view_info.get("id") or "")
        await client.views_update(view_id=view_id, view=view)  # pyright: ignore[reportUnknownMemberType]
    return True
