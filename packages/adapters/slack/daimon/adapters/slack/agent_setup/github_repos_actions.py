"""Read-only Slack GitHub panel navigation."""

from __future__ import annotations

from typing import Any, cast

from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup import github_repos
from daimon.adapters.slack.agent_setup.read import load_panel_roster
from daimon.adapters.slack.agent_setup.state import PanelMetadata
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import connect_link, load_grants_panel, sync_connect_admin
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from slack_sdk.web.async_client import AsyncWebClient

_ACTIONS = frozenset(
    {
        github_repos.ACTION_OPEN,
        github_repos.ACTION_BACK,
        github_repos.ACTION_CONNECT_LINK,
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
    """Open the repository list; stale edit buttons are not registered here."""
    action_id = str(action.get("action_id") or "")
    if action_id not in _ACTIONS:
        return False
    if action_id == github_repos.ACTION_CONNECT_LINK:
        return True
    if action_id == github_repos.ACTION_BACK:
        from daimon.adapters.slack.agent_setup.actions import load_details_view

        if meta.agent_name:
            view = await load_details_view(
                runtime,
                tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
                meta=meta,
                agent_name=meta.agent_name,
                is_admin=await resolve_is_admin(client, user_id=user_id),
            )
            if view is not None:
                await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                    view_id=str(cast("dict[str, Any]", payload.get("view") or {}).get("id") or ""),
                    view=view,
                )
        return True
    name = str(action.get("value") or meta.agent_name or "")
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    is_admin = await resolve_is_admin(client, user_id=user_id)
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
            return True
        panel = await load_grants_panel(
            session,
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.ma_agent_id),
            agent_name=name,
        )
    connect_url = None
    if is_admin:
        try:
            async with runtime.sessionmaker.begin() as session:
                await sync_connect_admin(
                    session,
                    tenant_id=tenant_id,
                    platform="slack",
                    platform_user_id=user_id,
                    verified_tenant_admin=True,
                )
                connect_url = await connect_link(
                    session,
                    settings=runtime.settings,
                    tenant_id=tenant_id,
                    platform="slack",
                    platform_user_id=user_id,
                    verified_tenant_admin=True,
                    agent_id=derive_agent_uuid(
                        tenant_id=tenant_id,
                        ma_agent_id=agent.ma_agent_id,
                    ),
                    agent_name=name,
                    agent_ma_id=agent.ma_agent_id,
                    origin_parent_channel_id=meta.channel_id or None,
                    origin_thread_id=meta.thread_id,
                )
        except ValueError:
            pass
    view = github_repos.build_view(
        meta.with_view("github_repos", agent_name=name), panel, connect_url=connect_url
    )
    await client.views_push(  # pyright: ignore[reportUnknownMemberType]
        trigger_id=str(payload.get("trigger_id") or ""),
        view=view,
    )
    return True
