"""Picture reset from the Slack agent details panel."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.panel_views import build_avatar_status_view
from daimon.adapters.slack.agent_setup.state import PanelMetadata, decode_panel_metadata
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_identity import CUSTOM_PICTURES_OFF, identity_enabled_for, is_builtin_agent
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.stores.agent_avatars import reset_avatar
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)


def evaluate_avatar_submission(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Answer a stale upload form with the refusal; nothing is read or written."""
    view: dict[str, Any] = payload.get("view") or {}
    meta = decode_panel_metadata(str(view.get("private_metadata") or ""))
    if meta is None:
        return None
    return {
        "response_action": "update",
        "view": build_avatar_status_view(meta=meta, message=CUSTOM_PICTURES_OFF),
    }


async def may_edit_avatar(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    team_id: str,
    user_id: str,
    agent_name: str,
) -> bool:
    if not identity_enabled_for(runtime.settings, "slack", team_id):
        return False
    if not await resolve_is_admin(client, user_id=user_id):
        return False
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=agent_name)
    return agent is not None and not is_builtin_agent(
        name=agent.name,
        metadata=agent.metadata,
        default_agent_name=runtime.deployment_default.agent_name,
    )


async def _audit(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    change: bool,
    outcome: PanelOutcome,
    reason: str,
    agent_name: str,
) -> None:
    await record_panel_write(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id=user_id,
        op="agent_avatar_change" if change else "agent_avatar_reset",
        outcome=outcome,
        reason=reason,
        agent_name=agent_name,
    )


async def _refresh_details(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    meta: PanelMetadata,
    notice: str | None = None,
) -> None:
    if not meta.root_view_id or not meta.agent_name:
        return
    from daimon.adapters.slack.agent_setup.actions import load_details_view

    view = await load_details_view(
        runtime, tenant_id=tenant_id, meta=meta, agent_name=meta.agent_name, is_admin=True
    )
    if view is not None:
        if notice is not None:
            view["blocks"].insert(
                0, {"type": "section", "text": {"type": "mrkdwn", "text": notice}}
            )
        await client.views_update(view_id=meta.root_view_id, view=view)  # pyright: ignore[reportUnknownMemberType]


async def reset_agent_avatar(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    meta: PanelMetadata,
    team_id: str,
    user_id: str,
    view_id: str,
) -> None:
    if not meta.agent_name or meta.team_id != team_id:
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=meta.team_id)
    if not await may_edit_avatar(
        runtime,
        client,
        tenant_id=tenant_id,
        team_id=team_id,
        user_id=user_id,
        agent_name=meta.agent_name,
    ):
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=user_id,
            change=False,
            outcome="denied",
            reason="needs_admin_or_agent_gone",
            agent_name=meta.agent_name,
        )
        if not identity_enabled_for(runtime.settings, "slack", team_id):
            await client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_avatar_status_view(meta=meta, message="Agent pictures are turned off."),
            )
        return
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )
        await reset_avatar(
            session,
            tenant_id=tenant_id,
            agent_name=meta.agent_name,
            updated_by_account_id=actor.account_id,
            face_enabled=True,
        )
    await _audit(
        runtime,
        tenant_id=tenant_id,
        user_id=user_id,
        change=False,
        outcome="allowed",
        reason="completed",
        agent_name=meta.agent_name,
    )
    await _refresh_details(
        runtime,
        client,
        tenant_id=tenant_id,
        meta=meta.with_view("details", agent_name=meta.agent_name, root_view_id=view_id),
        notice="Default picture restored.",
    )
