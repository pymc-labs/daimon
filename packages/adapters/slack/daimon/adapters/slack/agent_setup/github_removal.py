"""Show GitHub removal notices when an admin opens setup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from daimon.adapters.slack.agent_setup.github_link import send_link
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.github_panel import connect_link, sync_connect_admin
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.github_removal_notices import claim_notice, finish_notice
from slack_sdk.web.async_client import AsyncWebClient


async def send_pending_notice(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    channel_id: str,
    user_id: str,
    response_url: str | None = None,
) -> None:
    """Deliver one queued removal notice ephemerally in the setup channel."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    async with runtime.sessionmaker.begin() as session:
        notice = await claim_notice(session, tenant_id=tenant_id, now=datetime.now(UTC))
    if notice is None:
        return
    try:
        async with runtime.sessionmaker.begin() as session:
            await sync_connect_admin(
                session,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                verified_tenant_admin=True,
            )
            url = await connect_link(
                session,
                settings=runtime.settings,
                tenant_id=tenant_id,
                platform="slack",
                platform_user_id=user_id,
                verified_tenant_admin=True,
                origin_parent_channel_id=channel_id,
                origin_followup_token=response_url,
                origin_followup_expires_at=(
                    datetime.now(UTC) + timedelta(minutes=30) if response_url else None
                ),
            )
    except ValueError:
        async with runtime.sessionmaker.begin() as session:
            await finish_notice(session, notice=notice, delivered=False, now=datetime.now(UTC))
        return
    try:
        await send_link(
            client,
            channel_id=channel_id,
            user_id=user_id,
            url=url,
            line=f"GitHub connection to {notice.account_login} was removed. Reconnect GitHub here.",
        )
    except Exception:
        async with runtime.sessionmaker.begin() as session:
            await finish_notice(session, notice=notice, delivered=False, now=datetime.now(UTC))
        return
    async with runtime.sessionmaker.begin() as session:
        await finish_notice(session, notice=notice, delivered=True, now=datetime.now(UTC))
