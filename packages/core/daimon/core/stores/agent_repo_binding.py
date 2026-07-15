"""Per-agent git repo binding store. Phase 15 (INFRA-03)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import AgentRepoBinding
from daimon.core.errors import StoreError
from daimon.core.stores.domain import AgentRepoBindingRow
from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


def _normalize_owner_repo(url: str) -> str:
    """Extract `owner/repo` from a URL or short-form path.

    Accepts: 'https://github.com/owner/repo', 'github.com/owner/repo',
    'owner/repo', any with a trailing '/' or '.git'.

    Mirrors daimon.core.skill_sync.fetcher._normalize_owner_repo — both sides
    must normalize identically so set_binding and get_bindings_for_repo match
    (Pitfall 2 / T-56-09).
    """
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


async def set_binding(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
    default_branch: str,
    ma_secret_ref: str,
) -> AgentRepoBindingRow:
    """Upsert the per-agent repo binding (1:1, D-03). Returns the post-write row.

    Normalizes repo_url via _normalize_owner_repo before storing so the webhook
    reverse lookup (get_bindings_for_repo) always matches canonical 'owner/repo'.
    """
    normalized_url = _normalize_owner_repo(repo_url)
    stmt = (
        pg_insert(AgentRepoBinding)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url=normalized_url,
            default_branch=default_branch,
            ma_secret_ref=ma_secret_ref,
        )
        .on_conflict_do_update(
            constraint="pk_agent_repo_binding",
            set_={
                "repo_url": normalized_url,
                "default_branch": default_branch,
                "ma_secret_ref": ma_secret_ref,
                "updated_at": func.now(),
            },
        )
        .returning(AgentRepoBinding)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one()
    await session.flush()
    return AgentRepoBindingRow.model_validate(orm)


async def get_binding(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> AgentRepoBindingRow | None:
    """Return the per-(tenant, agent) binding, or None if unbound."""
    orm = await session.get(AgentRepoBinding, (tenant_id, agent_id))
    if orm is None:
        return None
    return AgentRepoBindingRow.model_validate(orm)


async def clear_binding(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Remove the binding. Raises StoreError when no binding exists."""
    result = await session.execute(
        delete(AgentRepoBinding).where(
            AgentRepoBinding.tenant_id == tenant_id,
            AgentRepoBinding.agent_id == agent_id,
        )
    )
    if cast(CursorResult[Any], result).rowcount == 0:
        raise StoreError(f"no binding for agent {agent_id}")
    await session.flush()


async def get_bindings_for_repo(
    session: AsyncSession,
    *,
    repo_url: str,
) -> list[AgentRepoBindingRow]:
    """Return all bindings whose repo_url matches the canonical form of repo_url.

    Install-agnostic (D-22): returns ALL tenants' bindings for the repo.
    Normalizes the lookup key the same way set_binding normalizes the write key,
    so a webhook's 'owner/repo' always matches stored rows (Pitfall 2 fix).
    """
    normalized = _normalize_owner_repo(repo_url)
    result = await session.execute(
        select(AgentRepoBinding).where(AgentRepoBinding.repo_url == normalized)
    )
    return [AgentRepoBindingRow.model_validate(o) for o in result.scalars()]


async def update_repo_and_branch_keep_secret(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
    default_branch: str,
) -> AgentRepoBindingRow:
    """Update repo_url/default_branch WITHOUT touching ma_secret_ref.

    Used on the keep-existing-token path (blank PAT) so a stored per-agent PAT
    reference is never clobbered by a repo-URL-only edit. Normalizes repo_url
    the same way set_binding does. Raises StoreError when no binding exists
    (mirrors update_last_sync / clear_binding discipline).
    """
    stmt = (
        update(AgentRepoBinding)
        .where(
            AgentRepoBinding.tenant_id == tenant_id,
            AgentRepoBinding.agent_id == agent_id,
        )
        .values(
            repo_url=_normalize_owner_repo(repo_url),
            default_branch=default_branch,
            updated_at=func.now(),
        )
        .returning(AgentRepoBinding)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        raise StoreError(f"no binding for agent {agent_id}")
    await session.flush()
    return AgentRepoBindingRow.model_validate(orm)


async def update_last_sync(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    last_sync_at: datetime,
    last_sync_error: str | None,
) -> AgentRepoBindingRow:
    """Persist last_sync_at / last_sync_error on the binding.

    Raises StoreError when no binding exists (mirrors clear_binding discipline).
    """
    stmt = (
        update(AgentRepoBinding)
        .where(
            AgentRepoBinding.tenant_id == tenant_id,
            AgentRepoBinding.agent_id == agent_id,
        )
        .values(last_sync_at=last_sync_at, last_sync_error=last_sync_error)
        .returning(AgentRepoBinding)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        raise StoreError(f"no binding for agent {agent_id}")
    await session.flush()
    return AgentRepoBindingRow.model_validate(orm)
