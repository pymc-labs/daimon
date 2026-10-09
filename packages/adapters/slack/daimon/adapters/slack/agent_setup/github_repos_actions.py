"""Slack click handlers for GitHub repo grants."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Literal, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_policy import refuse_unless_allowed_for_agent_name
from daimon.adapters.slack.agent_setup import github_add_repos, github_repos
from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.agent_setup.read import load_panel_roster
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import (
    GrantsPanel,
    activate_grants,
    connect_link,
    load_grants_panel,
    remove_panel_grant,
    safe_github_error,
    stage_panel_grant,
    sync_connect_admin,
)
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.stores.accounts import get_account
from daimon.core.stores.github_access import deactivate_agent
from daimon.core.stores.github_connect import CLIENT_AGENT_MESSAGE
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

_ACTIONS = frozenset(
    {
        github_repos.ACTION_OPEN,
        github_repos.ACTION_SELECT,
        github_repos.ACTION_STAGE,
        github_repos.ACTION_REMOVE,
        github_repos.ACTION_ACTIVATE,
        github_repos.ACTION_DEACTIVATE,
        github_repos.ACTION_PREVIOUS,
        github_repos.ACTION_NEXT,
        github_repos.ACTION_CONFIRM_REMOVE,
        github_repos.ACTION_CONFIRM_TURN_OFF,
        github_repos.ACTION_CANCEL_CONFIRM,
        github_repos.ACTION_ADD_OPEN,
        github_repos.ACTION_SETTINGS,
        github_repos.ACTION_SETTINGS_CHOICE,
        github_repos.ACTION_BACK,
        *github_add_repos.ACTIONS,
    }
)


async def _handle_add(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    payload: dict[str, Any],
    *,
    action: dict[str, Any],
    action_id: str,
    meta: PanelMetadata,
    panel: GrantsPanel,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: str,
    channel_id: str,
    push: bool,
    is_admin: bool,
) -> bool:
    """Advance the repo draft; commit all selected repos in one transaction."""
    selected = set(meta.selected_repo_ids or ())
    next_meta = dataclasses.replace(
        meta,
        view="github_add",
        agent_name=meta.agent_name,
        selected_repo_ids=tuple(sorted(selected)),
    )
    if action_id == github_add_repos.ACTION_SELECT:
        visible = {repo.repo_id for repo in panel.repos[meta.page * 20 : (meta.page + 1) * 20]}
        options = cast("list[dict[str, Any]]", action.get("selected_options") or [])
        try:
            chosen = {int(str(option.get("value") or "")) for option in options}
        except ValueError:
            return True
        if not chosen <= visible:
            return True
        selected = (selected - visible) | chosen
        if len(selected) > 100:
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text="Select up to 100 repos at a time.",
            )
            return True
        next_meta = dataclasses.replace(next_meta, selected_repo_ids=tuple(sorted(selected)))
    elif action_id == github_add_repos.ACTION_SUGGEST:
        try:
            suggested_id = int(str(action.get("value") or ""))
        except ValueError:
            return True
        from daimon.core.github_panel import suggested_repo

        suggestion = suggested_repo(panel.repos, meta.channel_name)
        if suggestion is None or suggestion.repo_id != suggested_id:
            return True
        selected.add(suggested_id)
        next_meta = dataclasses.replace(next_meta, selected_repo_ids=tuple(sorted(selected)))
    elif action_id == github_add_repos.ACTION_PREVIOUS:
        next_meta = dataclasses.replace(next_meta, page=max(0, meta.page - 1))
    elif action_id == github_add_repos.ACTION_NEXT:
        next_meta = dataclasses.replace(next_meta, page=meta.page + 1)
    elif action_id == github_add_repos.ACTION_CHANGE:
        next_meta = dataclasses.replace(next_meta, github_step="change")
    elif action_id == github_add_repos.ACTION_ABILITY:
        value = str(action.get("value") or "")
        if value not in ("read", "write"):
            return True
        next_meta = dataclasses.replace(
            next_meta,
            github_ability=value,
            github_step="pick",
        )
    elif action_id == github_add_repos.ACTION_BACK:
        if meta.github_step == "pick":
            view = github_repos.build_view(meta, panel)
            view_info = cast("dict[str, Any]", payload.get("view") or {})
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=str(view_info.get("id") or ""), view=view
            )
            return True
        next_meta = dataclasses.replace(next_meta, github_step="pick")
    elif action_id == github_add_repos.ACTION_CONNECT_MORE:
        if not is_admin:
            return True
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
                    agent_id=agent_id,
                    agent_name=meta.agent_name,
                    origin_parent_channel_id=channel_id,
                    origin_thread_id=meta.thread_id,
                )
        except ValueError as error:
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text=safe_github_error(error),
            )
            return True
        await send_link(
            client,
            channel_id=channel_id,
            thread_id=meta.thread_id,
            user_id=user_id,
            url=url,
            line=f"Connect GitHub for {meta.agent_name}.",
        )
        return True
    elif action_id == github_add_repos.ACTION_ADD:
        if not selected:
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text="Select at least one repo.",
            )
            return True
        if panel.saved_state:
            await post_ephemeral(
                client, channel_id=channel_id, user_id=user_id, text=CLIENT_AGENT_MESSAGE
            )
            return True
        return await _commit_add(
            runtime,
            client,
            payload,
            meta=next_meta,
            panel=panel,
            tenant_id=tenant_id,
            agent_id=agent_id,
            user_id=user_id,
            channel_id=channel_id,
        )
    view = github_add_repos.build_view(next_meta, panel, is_admin=is_admin)
    if push:
        await client.views_push(  # pyright: ignore[reportUnknownMemberType]
            trigger_id=str(payload.get("trigger_id") or ""), view=view
        )
    else:
        view_info = cast("dict[str, Any]", payload.get("view") or {})
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=str(view_info.get("id") or ""), view=view
        )
    return True


async def _commit_add(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    payload: dict[str, Any],
    *,
    meta: PanelMetadata,
    panel: GrantsPanel,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: str,
    channel_id: str,
) -> bool:
    selected = set(meta.selected_repo_ids or ())
    if not selected:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text="Select at least one repo.",
        )
        return True
    removed_key = False
    try:
        async with runtime.sessionmaker.begin() as session:
            principal = await get_or_create_platform_principal(
                session, tenant_id=tenant_id, platform="slack", external_id=user_id
            )
            account = await get_account(session, principal.account_id)
            if account is None or account.is_external:
                raise ValueError("You cannot change this agent's GitHub repos.")
            fresh = await load_grants_panel(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=meta.agent_name or ""
            )
            chosen = [repo for repo in fresh.repos if repo.repo_id in selected]
            if len(chosen) != len(selected):
                raise ValueError("A repo is no longer connected here. Review your choices.")
            for repo in chosen:
                ability: Literal["read", "write"] = (
                    "read"
                    if meta.github_ability == "read" or repo.max_access == "read"
                    else "write"
                )
                await stage_panel_grant(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    repo_id=repo.repo_id,
                    baseline_access=ability,
                    ceiling_access=ability,
                    account_id=account.id,
                    is_working_repo=repo.working
                    or (
                        fresh.working_repo is not None
                        and repo.full_name.casefold() == fresh.working_repo.casefold()
                    ),
                )
            removed_key = await activate_grants(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=account.id,
                agent_name=meta.agent_name or "",
            )
            fresh = await load_grants_panel(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=meta.agent_name or ""
            )
    except ValueError as error:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=safe_github_error(error),
        )
        return True
    view_info = cast("dict[str, Any]", payload.get("view") or {})
    await client.views_update(  # pyright: ignore[reportUnknownMemberType]
        view_id=str(view_info.get("id") or ""), view=github_repos.build_view(meta, fresh)
    )
    if removed_key:
        await post_ephemeral(
            client,
            channel_id=channel_id,
            user_id=user_id,
            text=f"Updated {meta.agent_name}. Open chats restart on the next turn.",
        )
    return True


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
    if action_id in github_add_repos.ACTIONS or action_id in (github_repos.ACTION_OPEN,):
        async with runtime.sessionmaker() as session:
            add_panel = await load_grants_panel(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=name
            )
        initial = action_id in (
            github_add_repos.ACTION_OPEN,
            github_repos.ACTION_OPEN,
        )
        if action_id != github_repos.ACTION_OPEN or (
            not add_panel.saved_state
            and not any(repo.live_ceiling is not None for repo in add_panel.repos)
        ):
            add_meta = (
                dataclasses.replace(
                    meta,
                    agent_name=name,
                    page=0,
                    selected_repo_ids=None,
                    github_ability="write",
                    github_step="pick",
                )
                if initial
                else meta
            )
            return await _handle_add(
                runtime,
                client,
                payload,
                action=action,
                action_id=action_id,
                meta=add_meta,
                panel=add_panel,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                channel_id=channel_id,
                push=action_id == github_repos.ACTION_OPEN,
                is_admin=is_admin,
            )
    selected_repo_id = meta.repo_id
    if action_id == github_repos.ACTION_BACK and meta.github_settings:
        async with runtime.sessionmaker() as session:
            panel = await load_grants_panel(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=name
            )
        view_info = cast("dict[str, Any]", payload.get("view") or {})
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=str(view_info.get("id") or ""),
            view=github_repos.build_view(
                dataclasses.replace(
                    meta,
                    github_settings=meta.github_step.startswith("settings_"),
                    github_step="pick",
                ),
                panel,
            ),
        )
        return True
    if action_id == github_repos.ACTION_BACK:
        from daimon.adapters.slack.agent_setup.actions import load_details_view

        details = await load_details_view(
            runtime, tenant_id=tenant_id, meta=meta, agent_name=name, is_admin=is_admin
        )
        if details is not None:
            view_info = cast("dict[str, Any]", payload.get("view") or {})
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=str(view_info.get("id") or ""), view=details
            )
        return True
    if action_id == github_repos.ACTION_SETTINGS:
        async with runtime.sessionmaker() as session:
            panel = await load_grants_panel(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=name
            )
        view_info = cast("dict[str, Any]", payload.get("view") or {})
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=str(view_info.get("id") or ""),
            view=github_repos.build_view(
                dataclasses.replace(meta, github_settings=True, github_step="pick"), panel
            ),
        )
        return True
    if action_id == github_repos.ACTION_SETTINGS_CHOICE:
        choice = str(action.get("value") or "")
        if choice not in ("ability", "remove"):
            return True
        async with runtime.sessionmaker() as session:
            panel = await load_grants_panel(
                session, tenant_id=tenant_id, agent_id=agent_id, agent_name=name
            )
        view_info = cast("dict[str, Any]", payload.get("view") or {})
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=str(view_info.get("id") or ""),
            view=github_repos.build_view(
                dataclasses.replace(meta, github_settings=True, github_step=f"settings_{choice}"),
                panel,
            ),
        )
        return True
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
                panel = await load_grants_panel(
                    session, tenant_id=tenant_id, agent_id=agent_id, agent_name=name
                )
                repo = next((r for r in panel.repos if r.repo_id == selected_repo_id), None)
                if action_id == github_repos.ACTION_STAGE:
                    if repo is None:
                        raise ValueError("Choose a connected repo first.")
                    field, level = str(action.get("value") or "").split(":", 1)
                    if field != "ceiling" or level not in ("read", "write"):
                        raise ValueError("That access choice is unavailable.")
                    await stage_panel_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        repo_id=repo.repo_id,
                        baseline_access=level,
                        ceiling_access=level,
                        account_id=actor,
                        is_working_repo=repo.working
                        or (
                            panel.working_repo is not None
                            and repo.full_name.casefold() == panel.working_repo.casefold()
                        ),
                    )
                    if panel.mode == "app":
                        await activate_grants(
                            session,
                            tenant_id=tenant_id,
                            agent_id=agent_id,
                            account_id=actor,
                            agent_name=name,
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
                            agent_name=name,
                        )
                elif action_id == github_repos.ACTION_ACTIVATE:
                    if panel.saved_state:
                        raise ValueError(CLIENT_AGENT_MESSAGE)
                    removed_pat = await activate_grants(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_id,
                        account_id=actor,
                        agent_name=name,
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
        except ValueError as error:
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text=safe_github_error(error),
            )
            return True
        if removed_pat:
            await post_ephemeral(
                client,
                channel_id=channel_id,
                user_id=user_id,
                text=f"Updated {name}. Open chats restart on the next turn.",
            )
    async with runtime.sessionmaker() as session:
        panel = await load_grants_panel(
            session, tenant_id=tenant_id, agent_id=agent_id, agent_name=name
        )
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
        meta,
        view="github_repos",
        agent_name=name,
        repo_id=selected_repo_id,
        page=next_page,
        github_settings=False if action_id == github_repos.ACTION_OPEN else meta.github_settings,
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
