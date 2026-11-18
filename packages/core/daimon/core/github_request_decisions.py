"""An admin's decision on a GitHub access request, shared by every chat platform.

The platform checks who clicked and renders its own screens; this does the
write and says what the requester's card should now show.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from daimon.core.config import Settings
from daimon.core.github_panel import connect_link, sync_connect_admin
from daimon.core.stores.github_access_requests import AccessRequest, set_status
from daimon.core.stores.github_links import account_link_status
from daimon.core.stores.github_personal_links import mint_link
from daimon.core.stores.github_request_actions import (
    approve_connected_request,
    approve_connection_request,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

WAITING_TEXT = "Waiting for GitHub confirmation."
DECLINED_TEXT = "An admin declined GitHub access for this request."
LINK_TEXT = "Link GitHub so Daimon can check that you have access to what this request needs."


def added_text(agent_name: str) -> str:
    return f"✓ Added. {agent_name} is continuing."


@dataclass(frozen=True)
class RequesterUpdate:
    """What the requester's card says after a decision, and its buttons."""

    text: str
    can_cancel: bool
    link_url: str | None = None


WAITING = RequesterUpdate(WAITING_TEXT, can_cancel=True)
DECLINED = RequesterUpdate(DECLINED_TEXT, can_cancel=True)


async def decline(
    sessionmaker: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID, request: AccessRequest
) -> bool:
    """Decline a request still in the status the admin saw."""
    async with sessionmaker.begin() as session:
        return await set_status(
            session,
            tenant_id=tenant_id,
            request_id=request.id,
            expected=request.status,
            status="declined",
        )


async def approve_connected(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """Grant a request whose repos are all connected. Raises `ValueError` with public text."""
    async with sessionmaker.begin() as session:
        return await approve_connected_request(
            session, tenant_id=tenant_id, request_id=request_id, account_id=account_id
        )


@dataclass(frozen=True)
class ConnectOrigin:
    """Where the GitHub connect link was asked for, so its result can be told back."""

    workspace_label: str | None
    requester_label: str | None
    parent_channel_id: str | None
    thread_id: str | None
    followup_token: str | None
    followup_expires_at: datetime | None


async def approve_with_connect(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    platform: str,
    admin_user_id: str,
    is_admin: bool,
    request_id: uuid.UUID,
    account_id: uuid.UUID,
    origin: ConnectOrigin,
) -> tuple[str, bool]:
    """Record the approval and return the admin's GitHub connect link and whether it changed.

    Raises `ValueError` with public text.
    """
    async with sessionmaker.begin() as session:
        await sync_connect_admin(
            session,
            tenant_id=tenant_id,
            platform=platform,
            platform_user_id=admin_user_id,
            is_platform_admin=is_admin,
        )
        url = await connect_link(
            session,
            settings=settings,
            tenant_id=tenant_id,
            platform=platform,
            platform_user_id=admin_user_id,
            verified_tenant_admin=is_admin,
            workspace_label=origin.workspace_label,
            requester_label=origin.requester_label,
            origin_parent_channel_id=origin.parent_channel_id,
            origin_thread_id=origin.thread_id,
            origin_followup_token=origin.followup_token,
            origin_followup_expires_at=origin.followup_expires_at,
        )
        changed = await approve_connection_request(
            session, tenant_id=tenant_id, request_id=request_id, account_id=account_id
        )
    return url, changed


async def approved_update(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    request: AccessRequest,
    *,
    tenant_id: uuid.UUID,
    platform: str,
) -> RequesterUpdate:
    """After a grant: ask a requester with no GitHub link to link one, else say it continues."""
    if settings.mcp.app_root_url is not None:
        async with sessionmaker() as session:
            linked = await account_link_status(session, account_id=request.requester_account_id)
        if not linked:
            async with sessionmaker.begin() as session:
                url = await mint_link(
                    session,
                    tenant_id=tenant_id,
                    account_id=request.requester_account_id,
                    platform=platform,
                    platform_user_id=request.requester_platform_user_id,
                    root_url=str(settings.mcp.app_root_url),
                )
            return RequesterUpdate(LINK_TEXT, can_cancel=True, link_url=url)
    return RequesterUpdate(added_text(request.agent_name), can_cancel=False)
