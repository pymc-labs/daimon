"""Shared GitHub connection actions for chat panels."""

from __future__ import annotations

import uuid
from datetime import datetime

from daimon.core.agent_reach import load_target_facts
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    confirm_stored_group_ids,
    grant_group_ids,
    read_stored_admin,
)
from daimon.core.config import Settings
from daimon.core.github_credentials import build_multifernet, decrypt_token, encrypt_token
from daimon.core.operation_policy import decide_operation
from daimon.core.scope import DeploymentDefault
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
    "can_manage_agent_github",
    "requester_manages_agent",
]

CONNECT_COPY = "Choose repos on GitHub. If someone else manages them, send them this link."


async def can_manage_agent_github(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    caller: ChannelAdminCaller,
    agent_names: tuple[str | None, ...],
    ma_agent_id: str,
    is_daimon_managed: bool,
    default: DeploymentDefault,
) -> bool:
    """Whether the caller may connect, grant and remove repos for one agent.

    A server admin, or a channel admin for a non-managed agent that is local to
    and held by their channels (`github_connect`, the `github_grant` terms).
    """
    facts = await load_target_facts(
        session,
        "github_connect",
        tenant_id=tenant_id,
        platform=platform,
        agent_names=agent_names,
        ma_agent_id=ma_agent_id,
        default=default,
        caller=caller,
        is_daimon_managed=is_daimon_managed,
        caller_platform_user_id=caller.platform_user_id,
    )
    return (
        decide_operation("github_connect", is_admin=caller.is_server_admin, target=facts) == "allow"
    )


async def requester_manages_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str | None,
    platform_user_id: str | None,
    agent_name: str | None,
    ma_agent_id: str | None,
    default: DeploymentDefault,
) -> bool:
    """The confirm-time check for a link someone made for one agent, outside a chat turn.

    Reads the requester's stored role and channel admin grants. A Slack or
    Teams group is not looked up here, so a grant held only through one no
    longer counts; a grant naming the person, or a Discord role, does.
    Whether the agent is managed was checked when the link was made.
    """
    if not agent_name or not ma_agent_id or platform is None:
        return False
    stored = await read_stored_admin(
        session,
        tenant_id=tenant_id,
        platform=platform,
        account_id=account_id,
        platform_user_id=platform_user_id,
    )
    if stored.is_admin:
        return True
    role_ids = await confirm_stored_group_ids(
        platform, platform_user_id, stored.role_ids, None, named=grant_group_ids(stored.grants)
    )
    return await can_manage_agent_github(
        session,
        tenant_id=tenant_id,
        platform=platform,
        caller=ChannelAdminCaller(platform_user_id=platform_user_id, role_ids=role_ids),
        agent_names=(agent_name,),
        ma_agent_id=ma_agent_id,
        is_daimon_managed=False,
        default=default,
    )


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
    agent_ma_id: str | None = None,
    verified_agent_manager: bool = False,
    origin_parent_channel_id: str | None = None,
    origin_thread_id: str | None = None,
    origin_followup_token: str | None = None,
    origin_followup_expires_at: datetime | None = None,
) -> str:
    """Mint for the clicker after resolving their current account.

    A server-wide link needs a live tenant admin. A link for one agent also
    allows someone the caller checked manages it (`can_manage_agent_github`);
    their repos are for that agent only.
    """
    root = connect_root(settings)
    if root is None:
        raise ValueError("GitHub connection is not configured for this workspace.")
    for_agent = agent_id is not None and agent_name is not None
    if verified_tenant_admin:
        account_id = await admin_account_for_platform_user(
            session, tenant_id=tenant_id, external_id=platform_user_id
        )
    elif for_agent and verified_agent_manager:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform=platform, external_id=platform_user_id
        )
        account_id = principal.account_id
    else:
        raise ValueError("Only a workspace admin can connect GitHub.")
    if agent_id is not None and agent_name is not None:
        await require_app_eligible_agent(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            agent_name=agent_name,
            switch_saved_key=True,
        )
    if start_over:
        await expire_pending_invitation(
            session,
            tenant_id=tenant_id,
            requester_account_id=account_id,
        )
    fernet = build_multifernet(tuple(key.get_secret_value() for key in settings.crypto.keys))
    token = await mint_invitation(
        session,
        tenant_id=tenant_id,
        requester_account_id=account_id,
        requester_label=requester_label or platform_user_id,
        requester_platform_user_id=platform_user_id,
        workspace_label=workspace_label,
        agent_id=agent_id,
        agent_name=agent_name,
        agent_ma_id=agent_ma_id,
        agent_manager_verified=verified_agent_manager,
        origin_platform=platform,
        origin_parent_channel_id=origin_parent_channel_id,
        origin_thread_id=origin_thread_id,
        encrypted_origin_followup=(
            encrypt_token(fernet, origin_followup_token) if origin_followup_token else None
        ),
        origin_followup_expires_at=origin_followup_expires_at,
    )
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
