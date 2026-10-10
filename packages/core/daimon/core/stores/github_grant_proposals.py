"""Short-lived, requester-bound proposals for conversational GitHub grants."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from daimon.core._models import GitHubGrantProposal
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.pending_skill_adds import is_affirmative
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

_LIFETIME = timedelta(minutes=15)


async def propose(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform_user_id: str,
    platform: str,
    thread_id: str,
    agent_id: uuid.UUID,
    repo_name: str,
    ability: Literal["read", "write", "remove"],
    origin_id: uuid.UUID,
) -> None:
    now = datetime.now(UTC)
    await session.execute(delete(GitHubGrantProposal).where(GitHubGrantProposal.expires_at <= now))
    statement = insert(GitHubGrantProposal).values(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        requester_account_id=account_id,
        requester_platform_user_id=platform_user_id,
        platform=platform,
        thread_id=thread_id,
        agent_id=agent_id,
        repo_name=repo_name.casefold(),
        ability=ability,
        origin_id=origin_id,
        created_at=now,
        expires_at=now + _LIFETIME,
    )
    await session.execute(
        statement.on_conflict_do_update(
            constraint="uq_github_grant_proposal",
            set_={
                "agent_id": agent_id,
                "repo_name": repo_name.casefold(),
                "ability": ability,
                "origin_id": origin_id,
                "approved_origin_id": None,
                "created_at": now,
                "expires_at": now + _LIFETIME,
            },
        )
    )


async def resolve(session: AsyncSession, *, origin: TurnOriginRow, message_text: str) -> None:
    """Use the adapter's human message, never a tool argument, to approve a proposal."""
    now = datetime.now(UTC)
    matching = (
        GitHubGrantProposal.tenant_id == origin.tenant_id,
        GitHubGrantProposal.requester_account_id == origin.account_id,
        GitHubGrantProposal.platform == origin.platform,
        GitHubGrantProposal.thread_id == origin.thread_id,
        GitHubGrantProposal.approved_origin_id.is_(None),
        GitHubGrantProposal.origin_id != origin.id,
        GitHubGrantProposal.created_at < origin.created_at,
    )
    if is_affirmative(message_text):
        await session.execute(
            update(GitHubGrantProposal)
            .where(*matching, GitHubGrantProposal.expires_at > now)
            .values(approved_origin_id=origin.id)
        )
    else:
        await session.execute(delete(GitHubGrantProposal).where(*matching))


async def consume(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform_user_id: str,
    platform: str,
    thread_id: str,
    agent_id: uuid.UUID,
    repo_name: str,
    ability: Literal["read", "write", "remove"],
    origin_id: uuid.UUID,
    origin_created_at: datetime,
) -> bool:
    now = datetime.now(UTC)
    row = await session.scalar(
        select(GitHubGrantProposal)
        .where(
            GitHubGrantProposal.tenant_id == tenant_id,
            GitHubGrantProposal.requester_account_id == account_id,
            GitHubGrantProposal.requester_platform_user_id == platform_user_id,
            GitHubGrantProposal.platform == platform,
            GitHubGrantProposal.thread_id == thread_id,
            GitHubGrantProposal.agent_id == agent_id,
            GitHubGrantProposal.repo_name == repo_name.casefold(),
            GitHubGrantProposal.ability == ability,
            GitHubGrantProposal.approved_origin_id == origin_id,
        )
        .with_for_update()
    )
    if (
        row is None
        or row.expires_at <= now
        or row.origin_id == origin_id
        or origin_created_at <= row.created_at
    ):
        return False
    await session.delete(row)
    await session.flush()
    return True
