"""Private Slack notice after GitHub removes an installation."""

from __future__ import annotations

from typing import cast

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.github_card_ui import github_card_blocks
from daimon.adapters.slack.interactions import resolve_web_client
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import connect_link, sync_connect_admin
from daimon.core.stores.github_access_requests import list_server_admin_recipients
from daimon.core.stores.github_removal_notices import RemovalNotice
from daimon.core.stores.tenants import get_tenant

_log = structlog.get_logger(__name__)


async def send_removal_dm(runtime: SlackRuntime, notice: RemovalNotice) -> bool:
    async with runtime.sessionmaker() as session:
        tenant = await get_tenant(session, notice.tenant_id)
        recipients = await list_server_admin_recipients(
            session, tenant_id=notice.tenant_id, platform="slack", limit=50
        )
    if tenant is None:
        return False
    client = await resolve_web_client(runtime, team_id=tenant.external_id)
    if client is None:
        return False
    landed = False
    for recipient in recipients:
        try:
            if not await resolve_is_admin(client, user_id=recipient.platform_user_id):
                continue
            async with runtime.sessionmaker.begin() as session:
                await sync_connect_admin(
                    session,
                    tenant_id=notice.tenant_id,
                    platform="slack",
                    platform_user_id=recipient.platform_user_id,
                    verified_tenant_admin=True,
                )
                url = await connect_link(
                    session,
                    settings=runtime.settings,
                    tenant_id=notice.tenant_id,
                    platform="slack",
                    platform_user_id=recipient.platform_user_id,
                    verified_tenant_admin=True,
                    workspace_label=tenant.external_id,
                    requester_label=None,
                )
            opened = await client.conversations_open(  # pyright: ignore[reportUnknownMemberType]
                users=recipient.platform_user_id
            )
            raw_channel: object = opened.get("channel")
            channel = (
                str(cast("dict[str, object]", raw_channel).get("id") or "")
                if isinstance(raw_channel, dict)
                else ""
            )
            if not channel:
                continue
            message = f"Daimon was removed from *{notice.account_login}* on GitHub."
            await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
                channel=channel,
                text=message,
                blocks=github_card_blocks(
                    message,
                    buttons=[
                        {
                            "type": "button",
                            "action_id": "github_removal__reconnect",
                            "text": {"type": "plain_text", "text": "Reconnect"},
                            "url": url,
                        }
                    ],
                ),
            )
            landed = True
        except Exception:
            _log.exception("github_removal.admin_dm_failed", tenant_id=str(notice.tenant_id))
    return landed
