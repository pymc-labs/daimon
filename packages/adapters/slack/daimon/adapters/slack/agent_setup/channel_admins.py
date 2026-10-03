"""The channel admins form's submission: who runs one channel besides the admins.

The form is pushed from Who answers where for workspace admins only. The
submission re-checks admin status live, saves or clears the grant, and
refreshes the routing view underneath. A grant names members and user groups;
a form that couldn't list the groups leaves the stored ones as they are.
"""

from __future__ import annotations

import dataclasses
import functools
from typing import Any

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.actions import (
    CHANNEL_ADMINS_NEED_ADMIN_MESSAGE,
    load_routing_view,
)
from daimon.adapters.slack.agent_setup.panel_views import (
    CHANNEL_ADMINS_GROUPS_INPUT_ID,
    CHANNEL_ADMINS_INPUT_ID,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata, decode_panel_metadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.channel_admins import InvalidChannelAdminIds, normalize_channel_admin_ids
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.panel_audit import record_panel_write
from daimon.core.stores.channel_admins import (
    delete_channel_admins,
    get_channel_admins,
    set_channel_admins,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()


@dataclasses.dataclass(frozen=True)
class ChannelAdminsSubmission:
    meta: PanelMetadata
    user_ids: tuple[str, ...]
    # None when the form had no group select: keep the stored groups.
    group_ids: tuple[str, ...] | None = None


def evaluate_channel_admins_submission(payload: dict[str, Any]) -> ChannelAdminsSubmission | None:
    """The form's panel metadata, members and groups, or None when unreadable. Pure."""
    view: dict[str, Any] = payload.get("view") or {}
    meta = decode_panel_metadata(str(view.get("private_metadata") or ""))
    if meta is None or not meta.channel_id:
        return None
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    block: dict[str, Any] = values.get(CHANNEL_ADMINS_INPUT_ID) or {}
    picked: dict[str, Any] = block.get(CHANNEL_ADMINS_INPUT_ID) or {}
    users: list[str] = picked.get("selected_users") or []
    groups_block: dict[str, Any] | None = values.get(CHANNEL_ADMINS_GROUPS_INPUT_ID)
    group_ids = None
    if groups_block is not None:
        groups_picked: dict[str, Any] = groups_block.get(CHANNEL_ADMINS_GROUPS_INPUT_ID) or {}
        options: list[dict[str, Any]] = groups_picked.get("selected_options") or []
        group_ids = tuple(str(option.get("value") or "") for option in options)
    return ChannelAdminsSubmission(
        meta=meta, user_ids=tuple(str(uid) for uid in users), group_ids=group_ids
    )


async def run_channel_admins_submission(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    submission: ChannelAdminsSubmission,
) -> None:
    """Save or clear the channel's admins, then refresh Who answers where."""
    meta = submission.meta
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    audit = functools.partial(
        record_panel_write,
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id=user_id,
        op="channel_admins",
    )

    async def refuse(text: str) -> None:
        await post_ephemeral(client, channel_id=meta.channel_id, user_id=user_id, text=text)

    if not await resolve_is_admin(client, user_id=user_id):
        await audit(outcome="denied", reason="needs_admin")
        await refuse(CHANNEL_ADMINS_NEED_ADMIN_MESSAGE)
        return
    async with runtime.sessionmaker.begin() as session:
        group_ids = submission.group_ids
        if group_ids is None:
            stored = await get_channel_admins(
                session, tenant_id=tenant_id, platform="slack", channel_id=meta.channel_id
            )
            group_ids = stored.role_ids if stored is not None else ()
        try:
            channel_id, groups, users = normalize_channel_admin_ids(
                "slack",
                channel_id=meta.channel_id,
                role_ids=group_ids,
                user_ids=submission.user_ids,
            )
        except InvalidChannelAdminIds as exc:
            await audit(outcome="error", reason="invalid_ids")
            await refuse(f"{exc}. Nothing changed.")
            return
        if users or groups:
            actor = await get_or_create_platform_principal(
                session, platform="slack", external_id=user_id, tenant_id=tenant_id
            )
            await set_channel_admins(
                session,
                tenant_id=tenant_id,
                platform="slack",
                channel_id=channel_id,
                role_ids=groups,
                user_ids=users,
                actor_account_id=actor.account_id,
            )
        else:
            await delete_channel_admins(
                session, tenant_id=tenant_id, platform="slack", channel_id=channel_id
            )
    await audit(outcome="allowed", reason="completed")
    log.info("slack.agent_setup.channel_admins.saved", users=len(users), groups=len(groups))
    if meta.root_view_id:
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=meta.root_view_id,
            view=await load_routing_view(
                runtime, client, tenant_id=tenant_id, meta=meta.with_view("routing"), is_admin=True
            ),
        )
