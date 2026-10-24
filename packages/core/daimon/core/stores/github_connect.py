"""Single-use GitHub connection invitations and browser flow records."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from cryptography.fernet import MultiFernet
from daimon.core._models import (
    Account,
    AccountGitHubLink,
    AgentGitHubGrant,
    AgentGitHubGrantDraft,
    CliPrincipal,
    GitHubAppInstallation,
    GitHubConnectClickIntent,
    GitHubConnectFlow,
    GitHubConnectInvitation,
    GitHubConnectRequest,
    PlatformPrincipal,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.continuity.continuation import sanitize_requested_work
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.stores import github_access
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.github_installation_coverage import installation_covers_repo
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.task_continuations import record_continuation
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

PINNED_AGENT_MESSAGE = "Connect this repository directly for the pinned agent."
CHANNEL_LIMITED_REPO_MESSAGE = (
    "This agent only works in some channels, so it can't use the shared repos. "
    "Ask whoever runs Daimon here to connect repos for it."
)


class PinnedAgentConnectionError(ValueError):
    """A pinned agent cannot use a repository connected server-wide."""


class GrantReassignmentError(ValueError):
    """A confirmed installation move would invalidate an existing agent grant set."""


async def require_app_eligible_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
) -> None:
    """A pinned agent may use only repositories connected for itself."""
    policy = await load_access_policy(session, tenant_id=tenant_id)
    rule = policy.agent_rules.get(agent_name)
    if (
        rule is not None
        and rule.runs_in is not None
        and await uses_server_wide_repo(session, tenant_id=tenant_id, agent_id=agent_id)
    ):
        raise PinnedAgentConnectionError(CHANNEL_LIMITED_REPO_MESSAGE)


async def uses_server_wide_repo(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> bool:
    """Whether a grant or a pending draft of the agent rests on a repo not connected for it."""
    for model in (AgentGitHubGrant, AgentGitHubGrantDraft):
        own = select(TenantGitHubRepo.id).where(
            TenantGitHubRepo.tenant_id == model.tenant_id,
            TenantGitHubRepo.repo_id == model.repo_id,
            TenantGitHubRepo.scope_agent_id == agent_id,
        )
        query = select(model.repo_id).where(
            model.tenant_id == tenant_id, model.agent_id == agent_id, ~own.exists()
        )
        if model is AgentGitHubGrantDraft:
            query = query.where(AgentGitHubGrantDraft.operation == "upsert")
        else:
            query = query.where(AgentGitHubGrant.app_visible.is_(True))
        if await session.scalar(query.limit(1)) is not None:
            return True
    return False


async def revoke_invitation(session: AsyncSession, *, token: str) -> None:
    """Discard a link that could not be delivered privately."""
    await session.execute(
        delete(GitHubConnectInvitation).where(GitHubConnectInvitation.token_hash == digest(token))
    )
    await session.flush()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _may_hold_link(
    account: Account | None, *, tenant_id: uuid.UUID, agent_id: uuid.UUID | None
) -> bool:
    """A workspace admin's link, or a member's link for one agent.

    Whether the member manages that agent is checked where the link is made
    and again when the repos are confirmed, by the caller of `confirm`.
    """
    return (
        account is not None
        and account.tenant_id == tenant_id
        and (account.role == "admin" or agent_id is not None)
    )


async def cli_account_id(
    session: AsyncSession, *, tenant_id: uuid.UUID, os_user: str
) -> uuid.UUID | None:
    return await session.scalar(
        select(CliPrincipal.account_id).where(
            CliPrincipal.tenant_id == tenant_id, CliPrincipal.os_user == os_user
        )
    )


async def admin_account_for_platform_user(
    session: AsyncSession, *, tenant_id: uuid.UUID, external_id: str
) -> uuid.UUID:
    """Resolve an invitation requester through the tenant's platform principal."""
    account_id = await session.scalar(
        select(Account.id)
        .join(PlatformPrincipal, PlatformPrincipal.account_id == Account.id)
        .join(Tenant, Tenant.id == Account.tenant_id)
        .where(
            Tenant.id == tenant_id,
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == Tenant.platform,
            PlatformPrincipal.external_id == external_id,
            Account.role == "admin",
        )
    )
    if account_id is None:
        raise ValueError("requester platform user ID must belong to a workspace admin")
    return account_id


class Invitation(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    token_hash: str
    tenant_id: uuid.UUID
    requester_account_id: uuid.UUID
    workspace_label: str
    requester_label: str
    requester_platform_user_id: str | None
    agent_id: uuid.UUID | None
    agent_name: str | None
    agent_ma_id: str | None = None
    operator_issued: bool
    connected_repo_count: int | None
    origin_platform: str | None
    origin_parent_channel_id: str | None
    origin_thread_id: str | None
    origin_ma_agent_id: str | None
    origin_responder_name: str | None
    requested_work: str | None
    requested_repo: str | None
    encrypted_origin_followup: bytes | None
    origin_followup_expires_at: datetime | None
    connected_repos: list[dict[str, str]] | None
    notice_claimed_at: datetime | None
    notice_delivered_at: datetime | None
    notice_attempts: int
    notice_next_attempt_at: datetime | None
    encrypted_token: bytes | None
    expires_at: datetime
    used_at: datetime | None


class Flow(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    state_hash: str
    invitation_hash: str
    cookie_hash: str
    encrypted_verifier: bytes
    encrypted_invitation_token: bytes | None
    encrypted_user_token: bytes | None
    github_user_id: int | None
    cancelled_at: datetime | None
    expires_at: datetime


async def mint_invitation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    requester_label: str | None = None,
    requester_platform_user_id: str | None = None,
    workspace_label: str | None = None,
    agent_id: uuid.UUID | None = None,
    agent_name: str | None = None,
    agent_ma_id: str | None = None,
    agent_manager_verified: bool = False,
    operator_issued: bool = False,
    origin_platform: str | None = None,
    origin_parent_channel_id: str | None = None,
    origin_thread_id: str | None = None,
    origin_ma_agent_id: str | None = None,
    origin_responder_name: str | None = None,
    requested_work: str | None = None,
    requested_repo: str | None = None,
    encrypted_origin_followup: bytes | None = None,
    origin_followup_expires_at: datetime | None = None,
) -> str:
    """Mint a single-use link. A non-admin needs an agent target the caller
    has checked they manage (`agent_manager_verified`)."""
    account = await session.get(Account, requester_account_id)
    if account is None or (
        account.role != "admin" and not (agent_id is not None and agent_manager_verified)
    ):
        raise ValueError("requester must be a tenant admin")
    if not _may_hold_link(account, tenant_id=tenant_id, agent_id=agent_id):
        raise ValueError("requester must be a tenant admin")
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise ValueError("workspace not found")
    if (agent_id is None) != (agent_name is None):
        raise ValueError("agent id and name must be supplied together")
    if agent_name is not None and agent_id is not None and not operator_issued:
        await require_app_eligible_agent(
            session, tenant_id=tenant_id, agent_id=agent_id, agent_name=agent_name
        )
    token = secrets.token_urlsafe(32)
    session.add(
        GitHubConnectInvitation(
            token_hash=digest(token),
            tenant_id=tenant_id,
            requester_account_id=requester_account_id,
            workspace_label=workspace_label
            or {"discord": "this Discord server", "teams": "this Teams organisation"}.get(
                tenant.platform, "this Slack workspace"
            ),
            requester_label=requester_label or str(requester_account_id),
            requester_platform_user_id=requester_platform_user_id,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_ma_id=agent_ma_id,
            operator_issued=operator_issued,
            origin_platform=origin_platform,
            origin_parent_channel_id=origin_parent_channel_id,
            origin_thread_id=origin_thread_id,
            origin_ma_agent_id=origin_ma_agent_id,
            origin_responder_name=origin_responder_name,
            requested_work=sanitize_requested_work(
                requested_work.strip()[:500] if requested_work else None,
                echoes=tuple(name for name in (agent_name, origin_responder_name) if name),
            ),
            requested_repo=requested_repo,
            encrypted_origin_followup=encrypted_origin_followup,
            origin_followup_expires_at=origin_followup_expires_at,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await session.flush()
    return token


async def record_connect_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
) -> None:
    """Keep one durable setup request per person and agent for the admin panel."""
    account = await session.get(Account, requester_account_id)
    if account is None or account.tenant_id != tenant_id:
        raise ValueError("requester must belong to this workspace")
    await session.execute(
        pg_insert(GitHubConnectRequest)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            requester_account_id=requester_account_id,
            agent_id=agent_id,
            agent_name=agent_name,
        )
        .on_conflict_do_update(
            constraint="uq_github_connect_request",
            set_={
                "agent_name": agent_name,
                "requested_at": datetime.now(UTC),
            },
        )
    )
    await session.flush()


async def count_requests_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(GitHubConnectRequest)
        .where(GitHubConnectRequest.requester_account_id == account_id)
    )
    return count or 0


async def delete_requests_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    result = await session.execute(
        delete(GitHubConnectRequest).where(GitHubConnectRequest.requester_account_id == account_id)
    )
    return cast(CursorResult[Any], result).rowcount


async def count_click_intents_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(GitHubConnectClickIntent)
        .where(GitHubConnectClickIntent.requester_account_id == account_id)
    )
    return count or 0


async def delete_click_intents_for_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    result = await session.execute(
        delete(GitHubConnectClickIntent).where(
            GitHubConnectClickIntent.requester_account_id == account_id
        )
    )
    return cast(CursorResult[Any], result).rowcount


async def sweep_expired_click_intents(session: AsyncSession, *, now: datetime) -> int:
    result = await session.execute(
        delete(GitHubConnectClickIntent).where(GitHubConnectClickIntent.expires_at <= now)
    )
    return cast(CursorResult[Any], result).rowcount


class ConfirmedActivation(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["activated"] = "activated"


async def activate_confirmed_agent(
    session: AsyncSession,
    *,
    invitation: Invitation,
    repos: list[RepoConfirmation],
    repo_locks_held: bool = False,
) -> ConfirmedActivation | None:
    """Grant confirmed repositories immediately to the selected agent."""
    await github_access.lock_tenant_grants(session, tenant_id=invitation.tenant_id)
    if invitation.agent_id is None:
        return None
    if not repo_locks_held:
        await github_access.lock_repo_grants_batch(
            session, tenant_id=invitation.tenant_id, repo_ids={repo.repo_id for repo in repos}
        )
    await require_app_eligible_agent(
        session,
        tenant_id=invitation.tenant_id,
        agent_id=invitation.agent_id,
        agent_name=invitation.agent_name or "",
    )
    granted = False
    for repo in repos:
        authorized = await github_access.repo_for_agent(
            session,
            tenant_id=invitation.tenant_id,
            repo_id=repo.repo_id,
            agent_id=invitation.agent_id,
        )
        if authorized is None or authorized.status != "active":
            continue
        existing = await session.get(
            AgentGitHubGrant, (invitation.tenant_id, invitation.agent_id, repo.repo_id)
        )
        await github_access.stage_grant(
            session,
            tenant_id=invitation.tenant_id,
            agent_id=invitation.agent_id,
            repo_id=repo.repo_id,
            baseline_access="write",
            ceiling_access="write",
            granted_by_account_id=invitation.requester_account_id,
            mount_path=existing.mount_path
            if existing is not None and existing.app_visible
            else None,
            is_working_repo=(
                existing.is_working_repo if existing is not None and existing.app_visible else False
            ),
            agent_name=invitation.agent_name,
            repo_lock_held=True,
        )
        granted = True
    if not granted:
        raise ValueError("No authorized repositories remain. Start a new GitHub connection.")
    await _drop_stale_grants(session, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id)
    await session.execute(
        delete(GitHubConnectRequest).where(
            GitHubConnectRequest.tenant_id == invitation.tenant_id,
            GitHubConnectRequest.agent_id == invitation.agent_id,
        )
    )
    return ConfirmedActivation()


async def queue_connect_followup(
    session: AsyncSession, *, invitation: Invitation, repos: list[RepoConfirmation]
) -> None:
    """Queue one private notice or one task continuation after activation commits.

    Teams has no notice poller, so only its continuation (a wake) reaches the chat.
    """
    if (
        invitation.operator_issued
        or invitation.origin_platform not in ("discord", "slack", "teams")
        or invitation.requester_platform_user_id is None
    ):
        return
    row = await session.get(GitHubConnectInvitation, invitation.token_hash, with_for_update=True)
    if row is None or row.used_at is None or row.connected_repos is not None:
        return
    granted: list[dict[str, str]] = []
    for repo in repos:
        if row.agent_id is None:
            granted.append({"name": repo.full_name, "access": repo.max_access})
            continue
        grant = await session.get(AgentGitHubGrant, (row.tenant_id, row.agent_id, repo.repo_id))
        if grant is not None and grant.app_visible:
            granted.append({"name": repo.full_name, "access": grant.ceiling_access})
    row.connected_repos = granted
    if not row.origin_thread_id:
        row.requested_work = None
    if (
        row.requested_work
        and row.origin_parent_channel_id
        and row.origin_thread_id
        and row.origin_ma_agent_id
        and row.origin_responder_name
    ):
        await record_continuation(
            session,
            tenant_id=row.tenant_id,
            platform=invitation.origin_platform,
            parent_channel_id=row.origin_parent_channel_id,
            thread_id=row.origin_thread_id,
            requester_account_id=row.requester_account_id,
            requester_external_user_id=invitation.requester_platform_user_id,
            target_ma_agent_id=row.origin_ma_agent_id,
            target_name=row.origin_responder_name,
            reason="github_access_ready",
            attended=True,
            idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, f"github-connect:{row.token_hash}"),
            requested_work=row.requested_work,
            available_at=datetime.now(UTC),
        )
    await session.flush()


async def _drop_stale_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    grants = await session.scalars(
        select(AgentGitHubGrant).where(
            AgentGitHubGrant.tenant_id == tenant_id,
            AgentGitHubGrant.agent_id == agent_id,
            AgentGitHubGrant.app_visible.is_(True),
        )
    )
    for grant in grants:
        repo = await github_access.repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
        installation = (
            await session.get(GitHubAppInstallation, repo.installation_id)
            if repo is not None
            else None
        )
        if (
            repo is None
            or repo.status != "active"
            or not installation_covers_repo(installation, repo.repo_full_name)
        ):
            await session.delete(grant)
    await session.flush()


async def get_invitation(session: AsyncSession, token_hash: str) -> Invitation | None:
    row = await session.get(GitHubConnectInvitation, token_hash)
    if row is None or row.used_at is not None or row.expires_at <= datetime.now(UTC):
        return None
    account = await session.get(Account, row.requester_account_id)
    if not _may_hold_link(account, tenant_id=row.tenant_id, agent_id=row.agent_id):
        return None
    return Invitation.model_validate(row)


async def invitation_status(
    session: AsyncSession, token_hash: str
) -> tuple[Literal["invalid", "used", "expired", "requester_left", "active"], Invitation | None]:
    row = await session.get(GitHubConnectInvitation, token_hash)
    if row is None:
        return "invalid", None
    invitation = Invitation.model_validate(row)
    if row.used_at is not None:
        return "used", invitation
    if row.expires_at <= datetime.now(UTC):
        return "expired", invitation
    account = await session.get(Account, row.requester_account_id)
    if not _may_hold_link(account, tenant_id=row.tenant_id, agent_id=row.agent_id):
        return "requester_left", invitation
    return "active", invitation


async def latest_pending_invitation(
    session: AsyncSession, *, tenant_id: uuid.UUID, requester_account_id: uuid.UUID
) -> Invitation | None:
    row = await session.scalar(
        select(GitHubConnectInvitation)
        .where(
            GitHubConnectInvitation.tenant_id == tenant_id,
            GitHubConnectInvitation.requester_account_id == requester_account_id,
            GitHubConnectInvitation.used_at.is_(None),
            GitHubConnectInvitation.expires_at > datetime.now(UTC),
            GitHubConnectInvitation.encrypted_token.is_not(None),
        )
        .order_by(GitHubConnectInvitation.expires_at.desc())
        .limit(1)
    )
    return Invitation.model_validate(row) if row is not None else None


async def set_invitation_encrypted_token(
    session: AsyncSession, *, token: str, encrypted_token: bytes
) -> None:
    row = await session.get(GitHubConnectInvitation, digest(token))
    if row is None:
        raise ValueError("GitHub connection link was not found.")
    row.encrypted_token = encrypted_token


async def create_discord_connect_intent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    requester_account_id: uuid.UUID,
    requester_platform_user_id: str,
    agent_id: uuid.UUID,
    agent_name: str,
    parent_channel_id: str,
    thread_id: str,
    origin_ma_agent_id: str,
    origin_responder_name: str,
    requested_work: str | None,
    requested_repo: str | None = None,
    agent_ma_id: str | None = None,
) -> uuid.UUID:
    await session.execute(
        delete(GitHubConnectClickIntent).where(
            GitHubConnectClickIntent.expires_at <= datetime.now(UTC)
        )
    )
    intent_id = uuid.uuid4()
    session.add(
        GitHubConnectClickIntent(
            id=intent_id,
            tenant_id=tenant_id,
            requester_account_id=requester_account_id,
            requester_platform_user_id=requester_platform_user_id,
            agent_id=agent_id,
            agent_name=agent_name,
            agent_ma_id=agent_ma_id,
            origin_parent_channel_id=parent_channel_id,
            origin_thread_id=thread_id,
            origin_ma_agent_id=origin_ma_agent_id,
            origin_responder_name=origin_responder_name,
            requested_work=requested_work,
            requested_repo=requested_repo,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await session.flush()
    return intent_id


async def revoke_discord_connect_intent(session: AsyncSession, *, intent_id: uuid.UUID) -> None:
    await session.execute(
        delete(GitHubConnectClickIntent).where(GitHubConnectClickIntent.id == intent_id)
    )


async def connect_intent_agent(
    session: AsyncSession, *, intent_id: uuid.UUID
) -> tuple[str, str | None] | None:
    """The (agent name, Managed Agents id) a live connect button is bound to."""
    intent = await session.get(GitHubConnectClickIntent, intent_id)
    if intent is None or intent.expires_at <= datetime.now(UTC):
        return None
    return intent.agent_name, intent.agent_ma_id


async def bind_discord_connect_click(
    session: AsyncSession,
    *,
    intent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    requester_platform_user_id: str,
    thread_id: str,
    fernet: MultiFernet,
    encrypted_followup: bytes,
    followup_expires_at: datetime,
    requester_manages_agent: bool = False,
) -> tuple[bytes, str | None] | None:
    """Mint once on the first authorized click and reuse that invitation.

    A clicker who is not a workspace admin needs the caller's live check that
    they manage the intent's agent (`requester_manages_agent`).
    """
    intent = await session.get(GitHubConnectClickIntent, intent_id, with_for_update=True)
    if (
        intent is None
        or intent.tenant_id != tenant_id
        or intent.requester_platform_user_id != requester_platform_user_id
        or intent.origin_thread_id != thread_id
        or intent.expires_at <= datetime.now(UTC)
    ):
        return None
    account = await session.get(Account, intent.requester_account_id)
    principal = await session.scalar(
        select(PlatformPrincipal).where(
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == "discord",
            PlatformPrincipal.external_id == requester_platform_user_id,
            PlatformPrincipal.account_id == intent.requester_account_id,
        )
    )
    if (
        not _may_hold_link(account, tenant_id=tenant_id, agent_id=intent.agent_id)
        or principal is None
    ):
        return None
    assert account is not None
    if account.role != "admin" and not requester_manages_agent:
        return None
    if intent.encrypted_token is None:
        token = await mint_invitation(
            session,
            tenant_id=tenant_id,
            requester_account_id=intent.requester_account_id,
            requester_platform_user_id=requester_platform_user_id,
            agent_id=intent.agent_id,
            agent_name=intent.agent_name,
            agent_ma_id=intent.agent_ma_id,
            agent_manager_verified=requester_manages_agent,
            origin_platform="discord",
            origin_parent_channel_id=intent.origin_parent_channel_id,
            origin_thread_id=intent.origin_thread_id,
            origin_ma_agent_id=intent.origin_ma_agent_id,
            origin_responder_name=intent.origin_responder_name,
            requested_work=intent.requested_work,
            requested_repo=intent.requested_repo,
        )
        encrypted_token = encrypt_token(fernet, token)
        intent.encrypted_token = encrypted_token
        await set_invitation_encrypted_token(session, token=token, encrypted_token=encrypted_token)
        await append_event(
            session,
            tenant_id=tenant_id,
            account_id=intent.requester_account_id,
            agent_id=intent.agent_id,
            platform="discord",
            platform_user_id=requester_platform_user_id,
            tool_name="github_connect",
            operation="github_connect",
            outcome="allowed",
            reason="admin link minted",
        )
    else:
        token = decrypt_token(fernet, intent.encrypted_token)
    row = await session.get(GitHubConnectInvitation, digest(token), with_for_update=True)
    if row is None or row.used_at is not None or row.expires_at <= datetime.now(UTC):
        return None
    row.encrypted_origin_followup = encrypted_followup
    row.origin_followup_expires_at = followup_expires_at
    await session.flush()
    return intent.encrypted_token, intent.agent_name


async def requester_linked_github_user_id(
    session: AsyncSession, *, account_id: uuid.UUID
) -> int | None:
    return await session.scalar(
        select(AccountGitHubLink.github_user_id).where(AccountGitHubLink.account_id == account_id)
    )


async def expire_pending_invitation(
    session: AsyncSession, *, tenant_id: uuid.UUID, requester_account_id: uuid.UUID
) -> None:
    row = await session.scalar(
        select(GitHubConnectInvitation)
        .where(
            GitHubConnectInvitation.tenant_id == tenant_id,
            GitHubConnectInvitation.requester_account_id == requester_account_id,
            GitHubConnectInvitation.used_at.is_(None),
            GitHubConnectInvitation.expires_at > datetime.now(UTC),
        )
        .order_by(GitHubConnectInvitation.expires_at.desc())
        .with_for_update()
        .limit(1)
    )
    if row is not None:
        row.expires_at = datetime.now(UTC)
        await session.flush()


async def successful_confirmation(
    session: AsyncSession, *, state: str, cookie: str = "", invitation_hash: str = ""
) -> Invitation | None:
    """Return a secret-free receipt for a used flow in this same browser."""
    if not state or (not cookie and not invitation_hash):
        return None
    flow = await session.get(GitHubConnectFlow, digest(state))
    if flow is None or flow.cancelled_at is not None or flow.expires_at <= datetime.now(UTC):
        return None
    if not (
        (cookie and flow.cookie_hash == digest(cookie)) or invitation_hash == flow.invitation_hash
    ):
        return None
    invitation = await session.get(GitHubConnectInvitation, flow.invitation_hash)
    if invitation is None or invitation.used_at is None or invitation.connected_repo_count is None:
        return None
    return Invitation.model_validate(invitation)


async def create_flow(
    session: AsyncSession,
    *,
    invitation_hash: str,
    state: str,
    cookie: str,
    encrypted_verifier: bytes,
    encrypted_invitation_token: bytes | None = None,
) -> None:
    await session.execute(
        delete(GitHubConnectFlow).where(
            GitHubConnectFlow.expires_at <= datetime.now(UTC),
            (GitHubConnectFlow.cancelled_at.is_(None))
            | (GitHubConnectFlow.encrypted_user_token.is_(None)),
        )
    )
    session.add(
        GitHubConnectFlow(
            state_hash=digest(state),
            invitation_hash=invitation_hash,
            cookie_hash=digest(cookie),
            encrypted_verifier=encrypted_verifier,
            encrypted_invitation_token=encrypted_invitation_token,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    await session.flush()


async def delete_expired_flows(session: AsyncSession, *, now: datetime, limit: int = 500) -> int:
    """Remove expired encrypted browser tokens in bounded scheduler batches."""
    expired = (
        select(GitHubConnectFlow.state_hash)
        .where(
            GitHubConnectFlow.expires_at <= now,
            (GitHubConnectFlow.cancelled_at.is_(None))
            | (GitHubConnectFlow.encrypted_user_token.is_(None)),
        )
        .limit(limit)
    )
    result = await session.execute(
        delete(GitHubConnectFlow).where(
            GitHubConnectFlow.state_hash.in_(expired),
            GitHubConnectFlow.expires_at <= now,
            (GitHubConnectFlow.cancelled_at.is_(None))
            | (GitHubConnectFlow.encrypted_user_token.is_(None)),
        )
    )
    return cast(CursorResult[Any], result).rowcount


async def get_flow(session: AsyncSession, *, state: str, cookie: str) -> Flow | None:
    if not state or not cookie:
        return None
    row = await session.get(GitHubConnectFlow, digest(state))
    if (
        row is None
        or row.cookie_hash != digest(cookie)
        or row.cancelled_at is not None
        or row.expires_at <= datetime.now(UTC)
    ):
        return None
    if await get_invitation(session, row.invitation_hash) is None:
        return None
    return Flow.model_validate(row)


async def set_user_token(
    session: AsyncSession, *, state: str, encrypted_token: bytes, github_user_id: int
) -> bool:
    row = await session.get(GitHubConnectFlow, digest(state), with_for_update=True)
    if (
        row is None
        or row.cancelled_at is not None
        or row.expires_at <= datetime.now(UTC)
        or row.encrypted_user_token is not None
    ):
        return False
    row.encrypted_user_token = encrypted_token
    row.github_user_id = github_user_id
    await session.flush()
    return True


async def cancel_flow(session: AsyncSession, *, state: str, cookie: str) -> Flow | None:
    """Block confirmation before token revocation, retaining failed work for retry."""
    if not state or not cookie:
        return None
    row = await session.get(GitHubConnectFlow, digest(state), with_for_update=True)
    if row is None or row.cookie_hash != digest(cookie):
        return None
    if row.cancelled_at is None:
        if row.expires_at <= datetime.now(UTC) or row.encrypted_user_token is None:
            return None
        row.cancelled_at = datetime.now(UTC)
        await session.flush()
    return Flow.model_validate(row)


async def finish_cancel_revocation(session: AsyncSession, *, state: str, cookie: str) -> None:
    """Forget the token only after GitHub confirms revocation."""
    row = await session.get(GitHubConnectFlow, digest(state), with_for_update=True)
    if row is not None and row.cookie_hash == digest(cookie) and row.cancelled_at is not None:
        row.encrypted_user_token = None
        await session.flush()


GitHubPermission = Literal["admin", "maintain", "write", "triage", "read"]


def github_permission(permissions: Mapping[str, bool]) -> GitHubPermission | None:
    """The highest permission in a GitHub repo's `permissions` object, or None.

    GitHub reports `admin`, `maintain`, `push` (write), `triage` and `pull`
    (read) as booleans; None means the person has no usable access.
    """
    for key, level in (
        ("admin", "admin"),
        ("maintain", "maintain"),
        ("push", "write"),
        ("triage", "triage"),
        ("pull", "read"),
    ):
        if permissions.get(key) is True:
            return cast(GitHubPermission, level)
    return None


class RepoConfirmation(BaseModel):
    repo_id: int
    owner_id: int
    installation_id: int
    full_name: str
    max_access: Literal["read", "write"]
    # The confirming person's GitHub permission on the repo; None when unknown.
    authorizer_permission: GitHubPermission | None = None


class StaleAgentReposError(ValueError):
    """The agent's grants changed after the browser page was rendered."""


async def agent_repo_snapshot(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, lock: bool = False
) -> str:
    """Canonical grant snapshot for the one-shot editor.

    A save locks grant and authorization rows before comparing. Callers also
    run the save in SERIALIZABLE isolation to detect an insert into an empty set.
    """
    query = (
        select(AgentGitHubGrant)
        .where(
            AgentGitHubGrant.tenant_id == tenant_id,
            AgentGitHubGrant.agent_id == agent_id,
            AgentGitHubGrant.app_visible.is_(True),
        )
        .order_by(AgentGitHubGrant.repo_id)
    )
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    grants = list(await session.scalars(query))
    values: list[tuple[Any, ...]] = []
    for grant in grants:
        repo = await github_access.repo_for_agent(
            session,
            tenant_id=tenant_id,
            repo_id=grant.repo_id,
            agent_id=agent_id,
            for_update=lock,
        )
        if lock and repo is not None:
            await session.refresh(repo)
        values.append(
            (
                grant.repo_id,
                grant.version,
                grant.baseline_access,
                grant.ceiling_access,
                grant.is_working_repo,
                grant.mount_path,
                None if repo is None else repo.version,
                None if repo is None else repo.status,
                None if repo is None else repo.repo_full_name,
            )
        )
    return json.dumps(values, separators=(",", ":"))


async def _require_reassignment_admission(
    session: AsyncSession,
    *,
    invitation: GitHubConnectInvitation,
    repos: list[RepoConfirmation],
) -> None:
    """Validate every affected agent before changing any confirmed repo row."""
    github_access.assert_tenant_grants_locked(session, tenant_id=invitation.tenant_id)
    repo_by_id = {repo.repo_id: repo for repo in repos}
    await github_access.lock_repo_grants_batch(
        session, tenant_id=invitation.tenant_id, repo_ids=set(repo_by_id)
    )
    agent_ids = await session.scalars(
        select(AgentGitHubGrant.agent_id)
        .where(
            AgentGitHubGrant.tenant_id == invitation.tenant_id,
            AgentGitHubGrant.repo_id.in_(repo_by_id),
            AgentGitHubGrant.app_visible.is_(True),
        )
        .distinct()
    )
    affected = sorted(set(agent_ids), key=str)
    for agent_id in affected:
        await github_access.lock_agent_grants(
            session, tenant_id=invitation.tenant_id, agent_id=agent_id
        )
    failures: list[str] = []
    for agent_id in affected:
        by_installation: dict[int, set[int]] = {}
        any_read_only = False
        has_working_repo = False
        moved: list[RepoConfirmation] = []
        for grant in await github_access.list_agent_grants(
            session, tenant_id=invitation.tenant_id, agent_id=agent_id
        ):
            current = await github_access.repo_for_agent(
                session, tenant_id=invitation.tenant_id, repo_id=grant.repo_id, agent_id=agent_id
            )
            proposed = repo_by_id.get(grant.repo_id)
            applies = proposed is not None and (
                invitation.agent_id == agent_id
                or (
                    invitation.agent_id is None
                    and (current is None or current.scope_agent_id is None)
                )
            )
            installation_id = (
                proposed.installation_id
                if applies and proposed is not None
                else current.installation_id
                if current is not None
                else None
            )
            if installation_id is None:
                continue
            by_installation.setdefault(installation_id, set()).add(grant.repo_id)
            any_read_only |= grant.ceiling_access != "write" or (
                current is not None and current.max_access != "write" and not applies
            )
            has_working_repo |= grant.is_working_repo
            if (
                applies
                and proposed is not None
                and (current is None or current.installation_id != proposed.installation_id)
            ):
                moved.append(proposed)
        if not moved:
            continue
        name = (
            invitation.agent_name
            if invitation.agent_id == agent_id and invitation.agent_name
            else await session.scalar(
                select(GitHubConnectInvitation.agent_name)
                .where(
                    GitHubConnectInvitation.tenant_id == invitation.tenant_id,
                    GitHubConnectInvitation.agent_id == agent_id,
                    GitHubConnectInvitation.agent_name.is_not(None),
                )
                .order_by(GitHubConnectInvitation.used_at.desc().nullslast())
                .limit(1)
            )
        ) or f"agent {agent_id}"
        try:
            github_access.require_projected_grant_budget(
                by_installation,
                any_read_only=any_read_only,
                has_working_repo=has_working_repo,
                agent_name=name,
            )
        except (
            github_access.InstallationLimitError,
            github_access.SessionVaultBudgetError,
            github_access.ReadOnlyMultiInstallationError,
        ) as exc:
            moved_repo = moved[0]
            owner = moved_repo.full_name.split("/", 1)[0]
            if isinstance(exc, github_access.ReadOnlyMultiInstallationError):
                remedy = (
                    "would have read-only GitHub repos across accounts or orgs. "
                    "Make them read-write or remove them first, then reconnect."
                )
            elif isinstance(exc, github_access.InstallationLimitError):
                remedy = (
                    "would exceed 12 GitHub accounts or orgs. "
                    "Remove a repo or an account first, then reconnect."
                )
            else:
                remedy = (
                    "would exceed the 15-credential limit for one conversation. "
                    "Remove a repo or an account first, then reconnect."
                )
            failures.append(f"{moved_repo.full_name} moved to {owner}. {name} {remedy}")
    if failures:
        raise GrantReassignmentError(" ".join(failures))


async def confirm(
    session: AsyncSession,
    *,
    state: str,
    cookie: str,
    github_user_id: int,
    repos: list[RepoConfirmation],
    requester_manages_agent: bool = False,
    expected_agent_snapshot: str | None = None,
) -> bool:
    """Consume the invitation and write confirmed rows in the caller's transaction.

    A link for one agent writes rows only that agent may use. A non-admin's
    link needs the caller to have checked again, in this transaction, that the
    requester still manages the agent (`requester_manages_agent`).
    """
    flow = await session.get(GitHubConnectFlow, digest(state))
    if flow is None:
        return False
    invitation = await session.get(GitHubConnectInvitation, flow.invitation_hash)
    if invitation is None:
        return False
    await github_access.lock_tenant_grants(session, tenant_id=invitation.tenant_id)
    github_access.assert_tenant_grants_locked(session, tenant_id=invitation.tenant_id)
    flow = await session.get(
        GitHubConnectFlow, digest(state), with_for_update=True, populate_existing=True
    )
    if (
        flow is None
        or flow.cookie_hash != digest(cookie)
        or flow.cancelled_at is not None
        or flow.expires_at <= datetime.now(UTC)
    ):
        return False
    invitation = await session.get(
        GitHubConnectInvitation, flow.invitation_hash, with_for_update=True, populate_existing=True
    )
    if (
        invitation is None
        or invitation.used_at is not None
        or invitation.expires_at <= datetime.now(UTC)
    ):
        return False
    account = await session.get(Account, invitation.requester_account_id, with_for_update=True)
    if not _may_hold_link(account, tenant_id=invitation.tenant_id, agent_id=invitation.agent_id):
        return False
    assert account is not None
    if account.role != "admin" and not requester_manages_agent:
        return False
    if len({repo.repo_id for repo in repos}) != len(repos):
        return False
    await _require_reassignment_admission(session, invitation=invitation, repos=repos)
    if expected_agent_snapshot is not None:
        if invitation.agent_id is None:
            return False
        current = await agent_repo_snapshot(
            session,
            tenant_id=invitation.tenant_id,
            agent_id=invitation.agent_id,
            lock=True,
        )
        if current != expected_agent_snapshot:
            raise StaleAgentReposError("The agent's repos changed. Reload and try again.")
    now = datetime.now(UTC)
    scope = invitation.agent_id
    for repo in repos:
        existing = await session.scalar(
            select(TenantGitHubRepo)
            .where(
                TenantGitHubRepo.tenant_id == invitation.tenant_id,
                TenantGitHubRepo.repo_id == repo.repo_id,
                TenantGitHubRepo.scope_agent_id.is_(None)
                if scope is None
                else TenantGitHubRepo.scope_agent_id == scope,
            )
            .with_for_update()
        )
        if existing is None:
            existing = TenantGitHubRepo(
                tenant_id=invitation.tenant_id, repo_id=repo.repo_id, scope_agent_id=scope
            )
            session.add(existing)
        else:
            existing.version += 1
        existing.owner_id = repo.owner_id
        existing.installation_id = repo.installation_id
        existing.repo_full_name = repo.full_name
        existing.max_access = "write"
        existing.authorized_by_github_user_id = github_user_id
        existing.authorized_by_account_id = invitation.requester_account_id
        existing.authorized_permission = repo.authorizer_permission
        existing.authorized_at = now
        existing.status = "active"
        existing.status_reason = None
    invitation.used_at = now
    invitation.connected_repo_count = len(repos)
    if invitation.encrypted_token is not None:
        await session.execute(
            delete(GitHubConnectClickIntent).where(
                GitHubConnectClickIntent.encrypted_token == invitation.encrypted_token
            )
        )
    await session.execute(
        update(GitHubConnectFlow)
        .where(
            GitHubConnectFlow.invitation_hash == flow.invitation_hash,
            GitHubConnectFlow.cancelled_at.is_(None),
        )
        .values(
            encrypted_verifier=b"",
            encrypted_invitation_token=None,
            encrypted_user_token=None,
            expires_at=now + timedelta(days=7),
        )
    )
    await session.flush()
    return True
