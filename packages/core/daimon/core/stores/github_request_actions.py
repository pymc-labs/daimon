"""Apply an admin's GitHub request decision and wake the waiting chat."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from daimon.core._models import (
    Account,
    AgentGitHubGrantDraft,
    GitHubAccessRequest,
    TenantGitHubRepo,
)
from daimon.core.stores import github_access
from daimon.core.stores.github_access import (
    InstallationLimitError,
    ReadOnlyMultiInstallationError,
    SessionVaultBudgetError,
)
from daimon.core.stores.github_access_requests import ready_and_continue
from daimon.core.stores.github_links import account_link_status
from daimon.core.stores.github_panel_grants import (
    activate_grants,
    load_grants_panel,
    stage_panel_grant,
)
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.task_continuations import record_continuation
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_confirmed_request_repos(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    approved_by_account_id: uuid.UUID,
    extra_repo_ids: set[int] | None = None,
    pending_requests: list[GitHubAccessRequest] | None = None,
) -> tuple[uuid.UUID, ...]:
    """Prelock all repos a confirmation may activate before it locks agents."""
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    requests = pending_requests
    if requests is None:
        requests = list(
            await session.scalars(
                select(GitHubAccessRequest).where(
                    GitHubAccessRequest.tenant_id == tenant_id,
                    GitHubAccessRequest.approved_by_account_id == approved_by_account_id,
                    GitHubAccessRequest.status == "waiting_github",
                    GitHubAccessRequest.expires_at > datetime.now(UTC),
                )
            )
        )
    names = {name.casefold() for request in requests for name in request.repo_names}
    agent_ids = {request.agent_id for request in requests}
    repo_ids = set(extra_repo_ids or ())
    if names:
        repos = await session.execute(
            select(TenantGitHubRepo.repo_id, TenantGitHubRepo.repo_full_name).where(
                TenantGitHubRepo.tenant_id == tenant_id
            )
        )
        repo_ids.update(repo_id for repo_id, name in repos if name.casefold() in names)
    if agent_ids:
        repo_ids.update(
            await session.scalars(
                select(AgentGitHubGrantDraft.repo_id).where(
                    AgentGitHubGrantDraft.tenant_id == tenant_id,
                    AgentGitHubGrantDraft.agent_id.in_(agent_ids),
                )
            )
        )
    await github_access.lock_repo_grants_batch(session, tenant_id=tenant_id, repo_ids=repo_ids)
    return tuple(request.id for request in requests)


async def approve_connected_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """Add every requested connected repo with today's limits, then resume once."""
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    actor = await session.get(Account, account_id)
    if actor is None or actor.tenant_id != tenant_id or actor.role != "admin":
        raise ValueError("Only an admin can approve this request.")
    request = await session.get(GitHubAccessRequest, request_id, with_for_update=True)
    if (
        request is None
        or request.tenant_id != tenant_id
        or request.status != "open"
        or request.expires_at <= datetime.now(UTC)
    ):
        return False
    requester = await session.get(Account, request.requester_account_id)
    if requester is None or requester.tenant_id != tenant_id:
        return False
    panel = await load_grants_panel(
        session, tenant_id=tenant_id, agent_id=request.agent_id, agent_name=request.agent_name
    )
    selected = [
        next(
            (repo for repo in panel.repos if repo.full_name.casefold() == name.casefold()),
            None,
        )
        for name in request.repo_names
    ]
    if any(repo is None for repo in selected):
        raise ValueError("Connect the repo first.")
    draft_ids = set(
        await session.scalars(
            select(AgentGitHubGrantDraft.repo_id).where(
                AgentGitHubGrantDraft.tenant_id == tenant_id,
                AgentGitHubGrantDraft.agent_id == request.agent_id,
            )
        )
    )
    await github_access.lock_repo_grants_batch(
        session,
        tenant_id=tenant_id,
        repo_ids=draft_ids | {repo.repo_id for repo in selected if repo is not None},
    )
    for repo in selected:
        assert repo is not None
        ceiling: Literal["write"] = "write"
        await stage_panel_grant(
            session,
            tenant_id=tenant_id,
            agent_id=request.agent_id,
            repo_id=repo.repo_id,
            baseline_access=ceiling,
            ceiling_access=ceiling,
            account_id=account_id,
            is_working_repo=repo.working,
        )
    await activate_grants(
        session,
        tenant_id=tenant_id,
        agent_id=request.agent_id,
        account_id=account_id,
        agent_name=request.agent_name,
    )
    request.approved_by_account_id = account_id
    if await account_link_status(session, account_id=requester.id):
        await ready_and_continue(session, tenant_id=tenant_id, request_id=request_id)
    elif request.requested_work:
        # Continue the same request once so it can show the personal link card.
        # The request stays open; linking later queues its final continuation.
        await record_continuation(
            session,
            tenant_id=tenant_id,
            platform=request.platform,
            parent_channel_id=request.parent_channel_id,
            thread_id=request.thread_id,
            requester_account_id=request.requester_account_id,
            requester_external_user_id=request.requester_platform_user_id,
            target_ma_agent_id=request.ma_agent_id,
            target_name=request.agent_name,
            reason="github_access_ready",
            attended=True,
            idempotency_key=uuid.uuid5(request.id, "agent-grant"),
            requested_work=request.requested_work,
            available_at=datetime.now(UTC),
        )
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=request.agent_id,
        platform=request.platform,
        platform_user_id=None,
        tool_name="github_access_request",
        operation="github_grant",
        outcome="allowed",
        reason="admin approved waiting GitHub request",
        github_repo_ids=[repo.repo_id for repo in selected if repo is not None],
    )
    return True


async def approve_connection_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """Remember the one admin decision while GitHub confirmation is pending."""
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    actor = await session.get(Account, account_id)
    if actor is None or actor.tenant_id != tenant_id or actor.role != "admin":
        raise ValueError("Only an admin can connect repos.")
    request = await session.get(GitHubAccessRequest, request_id, with_for_update=True)
    if (
        request is None
        or request.tenant_id != tenant_id
        or request.status != "open"
        or request.expires_at <= datetime.now(UTC)
    ):
        return False
    request.status = "waiting_github"
    request.approved_by_account_id = account_id
    request.updated_at = datetime.now(UTC)
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=request.agent_id,
        platform=request.platform,
        platform_user_id=None,
        tool_name="github_access_request",
        operation="github_connect",
        outcome="allowed",
        reason="admin approved waiting GitHub connection",
    )
    return True


async def finish_confirmed_requests(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    approved_by_account_id: uuid.UUID,
    prelocked_request_ids: tuple[uuid.UUID, ...] | None = None,
) -> int:
    """After GitHub confirms, add approved repos with the confirmed abilities."""
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    query = select(GitHubAccessRequest).where(
        GitHubAccessRequest.tenant_id == tenant_id,
        GitHubAccessRequest.approved_by_account_id == approved_by_account_id,
        GitHubAccessRequest.status == "waiting_github",
        GitHubAccessRequest.expires_at > datetime.now(UTC),
    )
    if prelocked_request_ids is not None:
        query = query.where(GitHubAccessRequest.id.in_(prelocked_request_ids))
    rows = list(await session.scalars(query.with_for_update(skip_locked=True)))
    if prelocked_request_ids is None:
        await lock_confirmed_request_repos(
            session,
            tenant_id=tenant_id,
            approved_by_account_id=approved_by_account_id,
            pending_requests=rows,
        )
    count = 0
    for row in rows:
        # A different confirmed ability or a still-missing repo needs a new decision.
        row.status = "open"
        row.approved_by_account_id = None
        try:
            async with session.begin_nested():
                if await approve_connected_request(
                    session,
                    tenant_id=tenant_id,
                    request_id=row.id,
                    account_id=approved_by_account_id,
                ):
                    count += 1
        except (InstallationLimitError, SessionVaultBudgetError, ReadOnlyMultiInstallationError):
            raise
        except ValueError:
            continue
    return count
