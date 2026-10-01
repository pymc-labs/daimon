"""Per-agent git repo binding store."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from daimon.core._models import AgentRepoBinding
from daimon.core.errors import StoreError
from daimon.core.stores.domain import AgentRepoBindingRow, RepoAccessProof, RepoProofKind
from sqlalchemy import CursorResult, case, delete, func, select, update
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
    proof: RepoAccessProof | None,
) -> AgentRepoBindingRow:
    """Upsert the per-agent repo binding (1:1). Returns the post-write row.

    Normalizes repo_url via _normalize_owner_repo before storing so the webhook
    reverse lookup (get_bindings_for_repo) always matches canonical 'owner/repo'.

    `proof` records what the caller established about repo access at bind
    time. It has no default: every caller must state what it established,
    because a write that silently elides proof is a write that should not
    happen. Passing `None` explicitly is the one legitimate way to record
    that no proof was established — pre-migration rows carry it, and clearing
    proof on a repo change is correct behavior — but it must be said, not
    defaulted into. Both the insert values and the conflict-path set_ clause
    write all three proof columns unconditionally, so a re-bind that states
    an explicitly-absent proof clears any stale proof rather than silently
    inheriting the previous bind's.
    """
    normalized_url = _normalize_owner_repo(repo_url)
    proof_kind = proof.kind if proof is not None else None
    proof_at = proof.at if proof is not None else None
    proof_account_id = proof.account_id if proof is not None else None
    stmt = (
        pg_insert(AgentRepoBinding)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url=normalized_url,
            default_branch=default_branch,
            ma_secret_ref=ma_secret_ref,
            proof_kind=proof_kind,
            proof_at=proof_at,
            proof_account_id=proof_account_id,
        )
        .on_conflict_do_update(
            constraint="pk_agent_repo_binding",
            set_={
                "repo_url": normalized_url,
                "default_branch": default_branch,
                "ma_secret_ref": ma_secret_ref,
                "proof_kind": proof_kind,
                "proof_at": proof_at,
                "proof_account_id": proof_account_id,
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

    Install-agnostic: returns ALL tenants' bindings for the repo.
    Normalizes the lookup key the same way set_binding normalizes the write key,
    so a webhook's 'owner/repo' always matches stored rows (Pitfall 2 fix).
    """
    normalized = _normalize_owner_repo(repo_url)
    result = await session.execute(
        select(AgentRepoBinding).where(AgentRepoBinding.repo_url == normalized)
    )
    return [AgentRepoBindingRow.model_validate(o) for o in result.scalars()]


async def get_tenant_repo_proof_kind(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    repo_url: str,
) -> RepoProofKind | None:
    """Return a recorded proof_kind THIS tenant established for repo_url, or None.

    Scans tenant_id's own bindings for repo_url (any agent) and returns the
    first non-null proof_kind found, or None if no such binding exists or
    none of them recorded proof. This is the evidence a skill-sync caller
    needs before treating GitHub App installation coverage as authorization
    to read a repo on this tenant's behalf: App coverage is installed by
    repo owners for their own use, not by the tenant syncing the repo, so it
    cannot stand in for a demonstrated access check (mirrors the reasoning
    in ``select_clone_auth``'s docstring).

    Used by ``select_skill_sync_auth``'s callers, which have no single
    binding row to key off of (a skill-sync URL may name a repo this tenant
    never bound at all) — they still need to know whether ANY of this
    tenant's own bindings already proved access to that specific repo.
    """
    bindings = await get_bindings_for_repo(session, repo_url=repo_url)
    for binding in bindings:
        if binding.tenant_id == tenant_id and binding.proof_kind is not None:
            return binding.proof_kind
    return None


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

    Proof is per-repo, so re-pointing a binding at a different repo
    invalidates it: the three proof columns are cleared when — and only
    when — the normalized repo URL actually changes. A branch-only edit
    leaves proof intact. This is computed atomically inside the same UPDATE
    via a CASE comparing against the pre-update column value, with no
    separate read.
    """
    normalized_url = _normalize_owner_repo(repo_url)
    url_unchanged = AgentRepoBinding.repo_url == normalized_url
    stmt = (
        update(AgentRepoBinding)
        .where(
            AgentRepoBinding.tenant_id == tenant_id,
            AgentRepoBinding.agent_id == agent_id,
        )
        .values(
            repo_url=normalized_url,
            default_branch=default_branch,
            proof_kind=case((url_unchanged, AgentRepoBinding.proof_kind), else_=None),
            proof_at=case((url_unchanged, AgentRepoBinding.proof_at), else_=None),
            proof_account_id=case((url_unchanged, AgentRepoBinding.proof_account_id), else_=None),
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


async def list_bindings_without_proof(session: AsyncSession) -> list[AgentRepoBindingRow]:
    """Return every binding with no recorded proof, ordered by repo_url.

    Install-agnostic: returns bindings from ALL tenants, matching
    get_bindings_for_repo's existing framing. Intended for an operator
    inventory of the population a fail-closed proof gate affects.
    """
    result = await session.execute(
        select(AgentRepoBinding)
        .where(AgentRepoBinding.proof_kind.is_(None))
        .order_by(AgentRepoBinding.repo_url)
    )
    return [AgentRepoBindingRow.model_validate(o) for o in result.scalars()]


async def record_proof(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    proof: RepoAccessProof,
) -> AgentRepoBindingRow:
    """Stamp a freshly-established proof onto an existing binding.

    Narrowed by construction: this can only UPDATE the three proof columns
    on a binding that already exists — it cannot create a binding, change
    `repo_url`, or touch `ma_secret_ref`. Its two callers are the
    keep-existing-token repo edit (which re-establishes proof against the
    new repo and records it in the same transaction) and the one-shot
    operator backfill (which records a `public` proof with a NULL account,
    having no acting person). `proof` is required and non-nullable: clearing
    proof is `update_repo_and_branch_keep_secret`'s job, not this one's.
    Raises StoreError when no binding exists (mirrors update_last_sync).
    """
    stmt = (
        update(AgentRepoBinding)
        .where(
            AgentRepoBinding.tenant_id == tenant_id,
            AgentRepoBinding.agent_id == agent_id,
        )
        .values(
            proof_kind=proof.kind,
            proof_at=proof.at,
            proof_account_id=proof.account_id,
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
