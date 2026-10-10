"""Single-use GitHub connection invitations and browser flow records."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from cryptography.fernet import MultiFernet
from daimon.core._models import (
    Account,
    AccountGitHubLink,
    AgentFile,
    AgentGitHubGrant,
    AgentGitHubGrantDraft,
    AgentRepoBinding,
    AgentSkillRepoCredential,
    CliPrincipal,
    GitHubAppInstallation,
    GitHubConnectClickIntent,
    GitHubConnectFlow,
    GitHubConnectInvitation,
    GitHubConnectRequest,
    PlatformPrincipal,
    Tenant,
    TenantGitHubRepo,
    ThreadSession,
)
from daimon.core.continuity.continuation import sanitize_requested_work
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import agent_files, agent_github_binding, github_access
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.github_credentials import delete_credential_for_principal
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.task_continuations import record_continuation
from daimon.core.stores.thread_session_lineage import request_fresh_start
from daimon.core.stores.thread_sessions import mark_github_key_restart
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

CLIENT_AGENT_MESSAGE = "This agent uses a saved GitHub key. Ask your Daimon operator to switch it."


class ClientAgentConnectionError(ValueError):
    """Self-serve GitHub setup cannot switch an agent's saved GitHub state."""


async def require_app_eligible_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
    switch_saved_key: bool = False,
) -> None:
    """Refuse a self-serve switch the operator owns.

    An agent pinned to channels by a rule may use only repos connected for it,
    never a server-wide one. `switch_saved_key` is for callers that retire the
    agent's saved key themselves when it switches (`activate_confirmed_agent`,
    `activate_grants`).
    """
    policy = await load_access_policy(session, tenant_id=tenant_id)
    rule = policy.agent_rules.get(agent_name)
    if (
        rule is not None
        and rule.runs_in is not None
        and await _uses_server_wide_repo(session, tenant_id=tenant_id, agent_id=agent_id)
    ):
        raise ClientAgentConnectionError(CLIENT_AGENT_MESSAGE)
    if await github_access.get_agent_mode(session, tenant_id=tenant_id, agent_id=agent_id) == "app":
        return
    if not switch_saved_key and await has_saved_github_state(
        session, tenant_id=tenant_id, agent_id=agent_id
    ):
        raise ClientAgentConnectionError(CLIENT_AGENT_MESSAGE)


async def _uses_server_wide_repo(
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
        if await session.scalar(query.limit(1)) is not None:
            return True
    return False


async def retire_saved_key(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> bool:
    """Remove the agent's per-agent PAT and token keys; open chats restart on their next turn."""
    overlay = await agent_github_binding.get_agent_github_binding(session, agent_id=agent_id)
    if overlay is not None:
        await agent_github_binding.delete_for_agent(session, agent_id=agent_id)
        if overlay.principal_id == agent_id:
            await delete_credential_for_principal(session, principal_id=agent_id)
    removed_token = False
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if await session.get(AgentFile, (tenant_id, agent_id, key)) is not None:
            await agent_files.delete_agent_file(
                session, tenant_id=tenant_id, agent_id=agent_id, key=key
            )
            removed_token = True
    if overlay is None and not removed_token:
        return False
    await mark_github_key_restart(session, tenant_id=tenant_id, agent_id=agent_id)
    return True


async def has_saved_github_state(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> bool:
    if await agent_github_binding.get_agent_github_binding(session, agent_id=agent_id):
        return True
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if await session.get(AgentFile, (tenant_id, agent_id, key)) is not None:
            return True
    if await session.get(AgentRepoBinding, (tenant_id, agent_id)) is not None:
        return True
    return (
        await session.scalar(
            select(AgentSkillRepoCredential.repo_url)
            .where(
                AgentSkillRepoCredential.tenant_id == tenant_id,
                AgentSkillRepoCredential.agent_id == agent_id,
            )
            .limit(1)
        )
        is not None
    )


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
        and not account.is_external
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
            Account.is_external.is_(False),
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
    activation_status: Literal["activated", "update_pending"] | None
    connected_repo_count: int | None
    origin_platform: str | None
    origin_parent_channel_id: str | None
    origin_thread_id: str | None
    origin_ma_agent_id: str | None
    origin_responder_name: str | None
    requested_work: str | None
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
    if agent_name is not None and agent_id is not None:
        await agent_files.lock_agent_keys(session, tenant_id=tenant_id, agent_id=agent_id)
        if not operator_issued:
            await require_app_eligible_agent(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                agent_name=agent_name,
                switch_saved_key=True,
            )
    token = secrets.token_urlsafe(32)
    session.add(
        GitHubConnectInvitation(
            token_hash=digest(token),
            tenant_id=tenant_id,
            requester_account_id=requester_account_id,
            workspace_label=workspace_label
            or ("this Discord server" if tenant.platform == "discord" else "this Slack workspace"),
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
    if account is None or account.tenant_id != tenant_id or account.is_external:
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


class MissingRepo(BaseModel):
    """A repo the agent needs before its saved key can be retired."""

    model_config = ConfigDict(frozen=True)
    full_name: str
    # The working repo needs read and write; a private skill repo, any access.
    needs_write: bool


class ConfirmedActivation(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["activated", "update_pending"]
    # True only when this activation removed a saved PAT or GH_TOKEN.
    retired_saved_key: bool = False
    # Why a chat Connect stayed `update_pending`: repos to add before the switch.
    missing_repos: tuple[MissingRepo, ...] = ()


async def activate_confirmed_agent(
    session: AsyncSession,
    *,
    invitation: Invitation,
    repos: list[RepoConfirmation],
) -> ConfirmedActivation | None:
    """Add the person's repos to one agent and switch it to them.

    Additive: grants the agent already has stay. An operator-issued update of
    a saved-key agent stays staged for the operator; a chat Connect, confirmed
    by someone who manages the agent, retires the saved key itself, but only
    when the agent's working repo (with write) and private skill repos are
    among its repos. Otherwise it stays `update_pending`, key kept, until the
    agent's GitHub panel saves a complete set (`activate_grants`).
    """
    if invitation.agent_id is None:
        return None
    agent_id = invitation.agent_id
    await agent_files.lock_agent_keys(session, tenant_id=invitation.tenant_id, agent_id=agent_id)
    if not invitation.operator_issued:
        await require_app_eligible_agent(
            session,
            tenant_id=invitation.tenant_id,
            agent_id=agent_id,
            agent_name=invitation.agent_name or "",
            switch_saved_key=True,
        )
    has_pat = await agent_github_binding.get_agent_github_binding(session, agent_id=agent_id)
    working = await session.get(AgentRepoBinding, (invitation.tenant_id, agent_id))
    skill_repos = list(
        await session.scalars(
            select(AgentSkillRepoCredential).where(
                AgentSkillRepoCredential.tenant_id == invitation.tenant_id,
                AgentSkillRepoCredential.agent_id == agent_id,
            )
        )
    )
    has_token_env = False
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        if await session.get(AgentFile, (invitation.tenant_id, agent_id, key)) is not None:
            has_token_env = True
            break
    app_active = (
        await github_access.get_agent_mode(
            session, tenant_id=invitation.tenant_id, agent_id=agent_id
        )
        == "app"
    )
    staged: list[RepoConfirmation] = []
    for repo in repos:
        authorized = await github_access.repo_for_agent(
            session, tenant_id=invitation.tenant_id, repo_id=repo.repo_id, agent_id=agent_id
        )
        if authorized is None or authorized.status != "active":
            continue
        existing = await session.get(
            AgentGitHubGrant, (invitation.tenant_id, agent_id, repo.repo_id)
        )
        await github_access.stage_grant(
            session,
            tenant_id=invitation.tenant_id,
            agent_id=agent_id,
            repo_id=repo.repo_id,
            baseline_access=repo.max_access,
            ceiling_access=repo.max_access,
            granted_by_account_id=invitation.requester_account_id,
            mount_path=existing.mount_path if existing is not None else None,
            is_working_repo=existing.is_working_repo if existing is not None else False,
        )
        staged.append(repo)
    if not staged:
        raise ValueError("No authorized repositories remain. Start a new GitHub connection.")
    await _drop_stale_grants(session, tenant_id=invitation.tenant_id, agent_id=agent_id)
    missing: list[MissingRepo] = []
    if not app_active:
        if invitation.operator_issued:
            await _check_required_repos(
                session,
                tenant_id=invitation.tenant_id,
                agent_id=agent_id,
                working=working,
                skill_repos=skill_repos,
            )
        else:
            # Switching now would cut the agent off its working or skill repo:
            # keep the saved key and stage the repos until those are added too.
            missing = await _missing_required_repos(
                session,
                tenant_id=invitation.tenant_id,
                agent_id=agent_id,
                working=working,
                skill_repos=skill_repos,
            )
    if missing or (
        invitation.operator_issued
        and not app_active
        and (has_pat is not None or has_token_env or working is not None or skill_repos)
    ):
        status: Literal["activated", "update_pending"] = "update_pending"
        retired = False
    else:
        await github_access.activate_agent(
            session,
            tenant_id=invitation.tenant_id,
            agent_id=agent_id,
            changed_by_account_id=invitation.requester_account_id,
        )
        retired = not app_active and await retire_saved_key(
            session, tenant_id=invitation.tenant_id, agent_id=agent_id
        )
        if retired:
            await append_event(
                session,
                tenant_id=invitation.tenant_id,
                account_id=invitation.requester_account_id,
                agent_id=agent_id,
                platform=None,
                platform_user_id=None,
                tool_name="github_connect",
                operation="github_grant",
                outcome="allowed",
                reason="saved key retired after confirmation",
            )
        await session.execute(
            delete(GitHubConnectRequest).where(
                GitHubConnectRequest.tenant_id == invitation.tenant_id,
                GitHubConnectRequest.agent_id == agent_id,
            )
        )
        status = "activated"
    row = await session.get(GitHubConnectInvitation, invitation.token_hash, with_for_update=True)
    if row is None or row.used_at is None:
        raise ValueError("connection was not confirmed")
    row.activation_status = status
    await session.flush()
    return ConfirmedActivation(
        status=status, retired_saved_key=retired, missing_repos=tuple(missing)
    )


async def queue_connect_followup(
    session: AsyncSession, *, invitation: Invitation, repos: list[RepoConfirmation]
) -> None:
    """Queue one private notice or one task continuation after activation commits."""
    if (
        invitation.operator_issued
        or invitation.origin_platform not in ("discord", "slack")
        or invitation.requester_platform_user_id is None
    ):
        return
    row = await session.get(GitHubConnectInvitation, invitation.token_hash, with_for_update=True)
    if (
        row is None
        or row.used_at is None
        or row.activation_status == "update_pending"
        or row.connected_repos is not None
    ):
        return
    granted: list[dict[str, str]] = []
    for repo in repos:
        if row.agent_id is None:
            granted.append({"name": repo.full_name, "access": repo.max_access})
            continue
        grant = await session.get(AgentGitHubGrant, (row.tenant_id, row.agent_id, repo.repo_id))
        if grant is not None and not grant.staged:
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
            idempotency_key=uuid.uuid5(uuid.NAMESPACE_URL, f"github-connect:{row.token_hash}"),
            requested_work=row.requested_work,
            available_at=datetime.now(UTC),
        )
    await session.flush()


async def activate_pending_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """Confirm a saved-key switch, then retire its key and restart open chats."""
    account = await session.get(Account, account_id)
    if (
        account is None
        or account.tenant_id != tenant_id
        or account.role != "admin"
        or account.is_external
    ):
        raise ValueError("An admin must confirm the update.")
    invitation = await session.scalar(
        select(GitHubConnectInvitation)
        .where(
            GitHubConnectInvitation.tenant_id == tenant_id,
            GitHubConnectInvitation.agent_id == agent_id,
            GitHubConnectInvitation.activation_status == "update_pending",
            GitHubConnectInvitation.operator_issued.is_(True),
        )
        .order_by(GitHubConnectInvitation.used_at.desc())
        .limit(1)
        .with_for_update()
    )
    if invitation is None:
        return False
    if not invitation.operator_issued:
        await require_app_eligible_agent(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            agent_name=invitation.agent_name or "",
        )
    await _drop_stale_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    await _check_required_repos(session, tenant_id=tenant_id, agent_id=agent_id)
    if not await session.scalar(
        select(AgentGitHubGrant.repo_id)
        .where(AgentGitHubGrant.tenant_id == tenant_id, AgentGitHubGrant.agent_id == agent_id)
        .limit(1)
    ):
        raise ValueError("No authorized repositories remain. Start a new GitHub connection.")
    await github_access.activate_agent(
        session, tenant_id=tenant_id, agent_id=agent_id, changed_by_account_id=account_id
    )
    overlay = await agent_github_binding.get_agent_github_binding(session, agent_id=agent_id)
    if overlay is not None:
        await agent_github_binding.delete_for_agent(session, agent_id=agent_id)
        if overlay.principal_id == agent_id:
            await delete_credential_for_principal(session, principal_id=agent_id)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        await agent_files.delete_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_id, key=key
        )
    sessions = await session.scalars(
        select(ThreadSession).where(
            ThreadSession.tenant_id == tenant_id,
            ThreadSession.status == "live",
            ThreadSession.ma_agent_id.is_not(None),
        )
    )
    now = datetime.now(UTC)
    for mapped in sessions:
        if (
            mapped.ma_agent_id is not None
            and derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=mapped.ma_agent_id) == agent_id
        ):
            await request_fresh_start(session, id=mapped.id, at=now)
    invitation.activation_status = "activated"
    await session.execute(
        delete(GitHubConnectRequest).where(
            GitHubConnectRequest.tenant_id == tenant_id,
            GitHubConnectRequest.agent_id == agent_id,
        )
    )
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="github_connect",
        operation="github_grant",
        outcome="allowed",
        reason="saved key retired after confirmation",
    )
    await session.flush()
    return True


async def _drop_stale_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    grants = await session.scalars(
        select(AgentGitHubGrant).where(
            AgentGitHubGrant.tenant_id == tenant_id, AgentGitHubGrant.agent_id == agent_id
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
            or installation is None
            or installation.suspended_at is not None
            or (grant.ceiling_access == "write" and repo.max_access != "write")
        ):
            await session.delete(grant)
    await session.flush()


async def _missing_required_repos(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    working: AgentRepoBinding | None = None,
    skill_repos: list[AgentSkillRepoCredential] | None = None,
) -> list[MissingRepo]:
    """The working and private skill repos switching credentials would strand."""
    grants = list(
        await session.scalars(
            select(AgentGitHubGrant).where(
                AgentGitHubGrant.tenant_id == tenant_id,
                AgentGitHubGrant.agent_id == agent_id,
            )
        )
    )
    connected: dict[str, AgentGitHubGrant] = {}
    for grant in grants:
        repo = await github_access.repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
        if repo is not None and repo.status == "active":
            connected[repo.repo_full_name.casefold()] = grant
    missing: list[MissingRepo] = []
    if working is None:
        working = await session.get(AgentRepoBinding, (tenant_id, agent_id))
    if working is not None:
        grant = connected.get(working.repo_url.casefold())
        if grant is None or grant.ceiling_access != "write":
            missing.append(MissingRepo(full_name=working.repo_url, needs_write=True))
    if skill_repos is None:
        skill_repos = list(
            await session.scalars(
                select(AgentSkillRepoCredential).where(
                    AgentSkillRepoCredential.tenant_id == tenant_id,
                    AgentSkillRepoCredential.agent_id == agent_id,
                )
            )
        )
    for skill in skill_repos:
        if skill.proof_kind != "public" and skill.repo_url.casefold() not in connected:
            missing.append(MissingRepo(full_name=skill.repo_url, needs_write=False))
    return missing


async def _check_required_repos(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    working: AgentRepoBinding | None = None,
    skill_repos: list[AgentSkillRepoCredential] | None = None,
) -> None:
    """Ensure switching credentials will not strand working or private skill repos."""
    missing = await _missing_required_repos(
        session, tenant_id=tenant_id, agent_id=agent_id, working=working, skill_repos=skill_repos
    )
    if any(repo.needs_write for repo in missing):
        raise ValueError("Connect the working repo with write access first.")
    if missing:
        raise ValueError("Connect the agent's skill repo first.")


async def has_pending_connect_update(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> bool:
    """A chat Connect confirmed for the agent but left staged to keep its saved key."""
    return (
        await session.scalar(
            select(GitHubConnectInvitation.token_hash)
            .where(
                GitHubConnectInvitation.tenant_id == tenant_id,
                GitHubConnectInvitation.agent_id == agent_id,
                GitHubConnectInvitation.activation_status == "update_pending",
                GitHubConnectInvitation.operator_issued.is_(False),
            )
            .limit(1)
        )
        is not None
    )


async def finish_pending_connect_updates(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    await session.execute(
        update(GitHubConnectInvitation)
        .where(
            GitHubConnectInvitation.tenant_id == tenant_id,
            GitHubConnectInvitation.agent_id == agent_id,
            GitHubConnectInvitation.activation_status == "update_pending",
            GitHubConnectInvitation.operator_issued.is_(False),
        )
        .values(activation_status="activated")
    )


async def pending_update_for_agent(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> Invitation | None:
    row = await session.scalar(
        select(GitHubConnectInvitation)
        .where(
            GitHubConnectInvitation.tenant_id == tenant_id,
            GitHubConnectInvitation.agent_id == agent_id,
            GitHubConnectInvitation.activation_status == "update_pending",
            GitHubConnectInvitation.operator_issued.is_(True),
        )
        .order_by(GitHubConnectInvitation.used_at.desc())
        .limit(1)
    )
    return Invitation.model_validate(row) if row is not None else None


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
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
    )
    await session.flush()
    return intent_id


async def revoke_discord_connect_intent(session: AsyncSession, *, intent_id: uuid.UUID) -> None:
    await session.execute(
        delete(GitHubConnectClickIntent).where(GitHubConnectClickIntent.id == intent_id)
    )


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


class RepoConfirmation(BaseModel):
    repo_id: int
    owner_id: int
    installation_id: int
    full_name: str
    max_access: Literal["read", "write"]


async def confirm(
    session: AsyncSession,
    *,
    state: str,
    cookie: str,
    github_user_id: int,
    repos: list[RepoConfirmation],
    requester_manages_agent: bool = False,
) -> bool:
    """Consume the invitation and write confirmed rows in the caller's transaction.

    A link for one agent writes rows only that agent may use. A non-admin's
    link needs the caller to have checked again, in this transaction, that the
    requester still manages the agent (`requester_manages_agent`).
    """
    flow = await session.get(GitHubConnectFlow, digest(state), with_for_update=True)
    if (
        flow is None
        or flow.cookie_hash != digest(cookie)
        or flow.cancelled_at is not None
        or flow.expires_at <= datetime.now(UTC)
    ):
        return False
    invitation = await session.get(
        GitHubConnectInvitation, flow.invitation_hash, with_for_update=True
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
            keep_higher = False
        else:
            keep_higher = existing.status == "active"
            existing.version += 1
        existing.owner_id = repo.owner_id
        existing.installation_id = repo.installation_id
        existing.repo_full_name = repo.full_name
        existing.max_access = (
            "write"
            if (keep_higher and existing.max_access == "write") or repo.max_access == "write"
            else "read"
        )
        existing.authorized_by_github_user_id = github_user_id
        existing.authorized_by_account_id = invitation.requester_account_id
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
