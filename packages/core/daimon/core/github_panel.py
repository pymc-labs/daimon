"""Shared GitHub connection actions for chat panels."""

from __future__ import annotations

import uuid

from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet, decrypt_token, encrypt_token
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_connect import (
    CLIENT_AGENT_MESSAGE,
    admin_account_for_platform_user,
    expire_pending_invitation,
    latest_pending_invitation,
    mint_invitation,
    require_app_eligible_agent,
    set_invitation_encrypted_token,
)
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
    "sync_connect_admin",
    "pending_connect_link",
]

CONNECT_COPY = "Choose repos on GitHub. If someone else manages them, send them this link."


async def sync_connect_admin(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    verified_tenant_admin: bool,
) -> None:
    """Record a live platform admin check before a connect entry point mints a link."""
    if not verified_tenant_admin:
        raise ValueError("Only a workspace admin can connect GitHub.")
    principal = await get_or_create_platform_principal(
        session, tenant_id=tenant_id, platform=platform, external_id=platform_user_id
    )
    await set_role(session, principal.account_id, Role.ADMIN)


_PUBLIC_ERRORS = frozenset(
    {
        "A repo is no longer connected here. Review your choices.",
        "Connect the working repo first.",
        "Give this agent read and write access to its working repo first.",
        "Connect the agent's skill repo first.",
        "Give this agent read access to its skill repo first.",
        "This repo is no longer connected.",
        "Select at least one repo.",
        "Only a server or workspace admin can manage connected repos.",
        "Only a workspace admin can connect GitHub.",
        CLIENT_AGENT_MESSAGE,
        "Confirm read and write access on GitHub first.",
    }
)


def safe_github_error(error: ValueError) -> str:
    """Keep internal access and storage terminology off chat screens."""
    message = str(error)
    return message if message in _PUBLIC_ERRORS else "GitHub didn't answer. Try again in a minute."


def suggested_repo(repos: tuple[RepoChoice, ...], channel_name: str | None) -> RepoChoice | None:
    """Suggest a connected repo whose name matches the channel without selecting it."""
    if not channel_name:
        return None
    slug = channel_name.lstrip("#").casefold().replace("_", "-")
    return next(
        (
            repo
            for repo in repos
            if repo.full_name.rsplit("/", 1)[-1].casefold().replace("_", "-") == slug
        ),
        None,
    )


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


async def pending_connect_link(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    requester_account_id: uuid.UUID,
) -> str | None:
    """Return this admin's still-live private link for `/github` resume."""
    root = connect_root(settings)
    if root is None:
        return None
    invitation = await latest_pending_invitation(
        session,
        tenant_id=tenant_id,
        requester_account_id=requester_account_id,
    )
    if invitation is None or invitation.encrypted_token is None:
        return None
    fernet = build_multifernet(tuple(key.get_secret_value() for key in settings.crypto.keys))
    token = decrypt_token(fernet, invitation.encrypted_token)
    return f"{root}/oauth/github/connect/{token}"


async def connect_link(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    verified_tenant_admin: bool,
    workspace_label: str | None = None,
    requester_label: str | None = None,
    start_over: bool = False,
    agent_id: uuid.UUID | None = None,
    agent_name: str | None = None,
) -> str:
    """Mint for the clicker after resolving their current tenant-admin account."""
    root = connect_root(settings)
    if root is None:
        raise ValueError("GitHub connection is not configured for this workspace.")
    if not verified_tenant_admin:
        raise ValueError("Only a workspace admin can connect GitHub.")
    account_id = await admin_account_for_platform_user(
        session, tenant_id=tenant_id, external_id=platform_user_id
    )
    if agent_id is not None and agent_name is not None:
        await require_app_eligible_agent(
            session, tenant_id=tenant_id, agent_id=agent_id, agent_name=agent_name
        )
    if start_over:
        await expire_pending_invitation(
            session,
            tenant_id=tenant_id,
            requester_account_id=account_id,
        )
    token = await mint_invitation(
        session,
        tenant_id=tenant_id,
        requester_account_id=account_id,
        requester_label=requester_label or platform_user_id,
        workspace_label=workspace_label,
        agent_id=agent_id,
        agent_name=agent_name,
    )
    fernet = build_multifernet(tuple(key.get_secret_value() for key in settings.crypto.keys))
    await set_invitation_encrypted_token(
        session, token=token, encrypted_token=encrypt_token(fernet, token)
    )
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        platform=platform,
        platform_user_id=platform_user_id,
        tool_name="github_connect",
        operation="github_connect",
        outcome="allowed",
        reason="invitation minted",
    )
    return f"{root}/oauth/github/connect/{token}"
