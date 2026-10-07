"""Shared GitHub connection actions for chat panels."""

from __future__ import annotations

import uuid

from daimon.core.config import Settings
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import admin_account_for_platform_user, mint_invitation
from daimon.core.stores.github_panel_grants import (
    GrantsPanel,
    RepoChoice,
    activate_grants,
    load_grants_panel,
    remove_panel_grant,
    stage_panel_grant,
)
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.security_audit import append_event
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "GrantsPanel",
    "RepoChoice",
    "activate_grants",
    "load_grants_panel",
    "remove_panel_grant",
    "stage_panel_grant",
    "CONNECT_COPY",
    "connect_root",
    "connect_link",
]

CONNECT_COPY = "Choose repos on GitHub. If someone else manages them, send them this link."


def connect_root(settings: Settings) -> str | None:
    """Return the browser route only when the complete App flow is configured."""
    app = settings.github_app
    root = settings.mcp.app_root_url
    if (
        root is None
        or app.app_id is None
        or app.app_slug is None
        or app.private_key is None
        or app.client_id is None
        or app.client_secret is None
        or not settings.crypto.keys
    ):
        return None
    return root


async def connect_link(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    preselected_repo_full_name: str | None = None,
) -> str:
    """Mint for the clicker after resolving their current tenant-admin account."""
    root = connect_root(settings)
    if root is None:
        raise ValueError("GitHub connection is not configured for this workspace.")
    principal = await get_or_create_platform_principal(
        session, tenant_id=tenant_id, platform=platform, external_id=platform_user_id
    )
    # The adapter checked the platform's live admin role for this click.
    await set_role(session, principal.account_id, Role.ADMIN)
    account_id = await admin_account_for_platform_user(
        session, tenant_id=tenant_id, external_id=platform_user_id
    )
    token = await mint_invitation(
        session,
        tenant_id=tenant_id,
        requester_account_id=account_id,
        requester_label=platform_user_id,
        preselected_repo_full_name=preselected_repo_full_name,
    )
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=None,
        platform=platform,
        platform_user_id=platform_user_id,
        tool_name="github_connect",
        operation="github_connect",
        outcome="allowed",
        reason="invitation minted",
    )
    return f"{root}/oauth/github/connect/{token}"
