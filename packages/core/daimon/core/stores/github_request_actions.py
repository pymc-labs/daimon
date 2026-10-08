"""Apply an admin's GitHub request decision and wake the waiting chat."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from daimon.core._models import Account, GitHubAccessRequest
from daimon.core.stores.github_access_requests import ready_and_continue
from daimon.core.stores.github_links import account_link_status
from daimon.core.stores.github_panel_grants import (
    activate_grants,
    load_grants_panel,
    stage_panel_grant,
)
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.task_continuations import record_continuation
from sqlalchemy.ext.asyncio import AsyncSession


async def approve_connected_request(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """Add every requested connected repo with today's limits, then resume once."""
    actor = await session.get(Account, account_id)
    if actor is None or actor.tenant_id != tenant_id or actor.is_external or actor.role != "admin":
        raise ValueError("Only a server or workspace admin can approve this request.")
    request = await session.get(GitHubAccessRequest, request_id, with_for_update=True)
    if (
        request is None
        or request.tenant_id != tenant_id
        or request.status != "open"
        or request.expires_at <= datetime.now(UTC)
    ):
        return False
    requester = await session.get(Account, request.requester_account_id)
    if requester is None or requester.is_external or requester.tenant_id != tenant_id:
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
    rank = {"read": 1, "write": 2}
    if any(
        repo is not None and rank[repo.max_access] < rank[request.required_ability]
        for repo in selected
    ):
        raise ValueError("GitHub confirmed read only. Review what this agent can do.")
    for repo in selected:
        assert repo is not None
        if request.required_ability not in ("read", "write"):
            raise ValueError("Requested access is invalid.")
        ceiling: Literal["read", "write"] = request.required_ability
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
    actor = await session.get(Account, account_id)
    if actor is None or actor.tenant_id != tenant_id or actor.is_external or actor.role != "admin":
        raise ValueError("Only a server or workspace admin can connect repos.")
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
) -> int:
    """After GitHub confirms, add approved repos with the confirmed abilities."""
    from sqlalchemy import select

    rows = await session.scalars(
        select(GitHubAccessRequest)
        .where(
            GitHubAccessRequest.tenant_id == tenant_id,
            GitHubAccessRequest.approved_by_account_id == approved_by_account_id,
            GitHubAccessRequest.status == "waiting_github",
            GitHubAccessRequest.expires_at > datetime.now(UTC),
        )
        .with_for_update(skip_locked=True)
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
        except ValueError:
            continue
    return count
