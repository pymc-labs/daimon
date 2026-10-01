"""Per-(tenant, agent, repo) skill-repo credential store.

Separate from `agent_repo_binding` on purpose: an agent has one working repo
but any number of skill repos, so enrolling a skill repo must never move the
binding the agent clones. See `AgentSkillRepoCredential`'s ORM docstring.
"""

from __future__ import annotations

import uuid

from daimon.core._models import AgentSkillRepoCredential
from daimon.core.stores.domain import (
    AgentSkillRepoCredentialRow,
    RepoAccessProof,
    RepoProofKind,
)
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


def _normalize_owner_repo(url: str) -> str:
    """Extract `owner/repo` from a URL or short-form path.

    Accepts: 'https://github.com/owner/repo', 'github.com/owner/repo',
    'owner/repo', any with a trailing '/' or '.git'.

    Copied verbatim from `agent_repo_binding._normalize_owner_repo` (itself a
    copy of `daimon.core.skill_sync.fetcher`'s). The duplication is the house
    pattern for this helper and its reason is the same: every store that keys
    rows by repo must normalize identically, and a shared import would make
    one module's change silently repoint another's lookups.
    """
    return (
        url.removeprefix("https://github.com/")
        .removeprefix("http://github.com/")
        .removeprefix("github.com/")
        .removesuffix(".git")
        .rstrip("/")
    )


async def set_skill_repo_credential(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
    default_branch: str,
    path: str,
    ma_secret_ref: str,
    proof: RepoAccessProof | None,
) -> AgentSkillRepoCredentialRow:
    """Upsert the skill-repo credential for (tenant, agent, repo). Post-write row.

    Normalizes `repo_url` before storing so a later lookup by 'owner/repo'
    matches. `proof` has no default, mirroring `agent_repo_binding.set_binding`:
    a caller must state what it established about read access, and passing
    `None` explicitly is the one way to record that nothing was. The conflict
    path writes all three proof columns unconditionally, so a re-enrollment
    stating an absent proof clears a stale one rather than inheriting it.
    """
    normalized_url = _normalize_owner_repo(repo_url)
    proof_kind = proof.kind if proof is not None else None
    proof_at = proof.at if proof is not None else None
    proof_account_id = proof.account_id if proof is not None else None
    stmt = (
        pg_insert(AgentSkillRepoCredential)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url=normalized_url,
            default_branch=default_branch,
            path=path,
            ma_secret_ref=ma_secret_ref,
            proof_kind=proof_kind,
            proof_at=proof_at,
            proof_account_id=proof_account_id,
        )
        .on_conflict_do_update(
            constraint="pk_agent_skill_repo_credentials",
            set_={
                "default_branch": default_branch,
                "path": path,
                "ma_secret_ref": ma_secret_ref,
                "proof_kind": proof_kind,
                "proof_at": proof_at,
                "proof_account_id": proof_account_id,
                "updated_at": func.now(),
            },
        )
        .returning(AgentSkillRepoCredential)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one()
    await session.flush()
    return AgentSkillRepoCredentialRow.model_validate(orm)


async def get_skill_repo_credential(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
) -> AgentSkillRepoCredentialRow | None:
    """Return the credential for (tenant, agent, repo), or None if unenrolled.

    Normalizes the lookup key exactly as the write path does.
    """
    orm = await session.get(
        AgentSkillRepoCredential, (tenant_id, agent_id, _normalize_owner_repo(repo_url))
    )
    if orm is None:
        return None
    return AgentSkillRepoCredentialRow.model_validate(orm)


async def list_skill_repo_credentials_for_repo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    repo_url: str,
) -> list[AgentSkillRepoCredentialRow]:
    """Return this tenant's credentials for `repo_url`, across all its agents.

    Tenant-scoped, unlike `agent_repo_binding.get_bindings_for_repo`: that one
    is install-agnostic because a GitHub webhook arrives with no tenant, while
    every caller here already knows whose skills it is syncing, and a
    cross-tenant read would leak which other installs enrolled the same repo.
    """
    normalized = _normalize_owner_repo(repo_url)
    result = await session.execute(
        select(AgentSkillRepoCredential)
        .where(
            AgentSkillRepoCredential.tenant_id == tenant_id,
            AgentSkillRepoCredential.repo_url == normalized,
        )
        .order_by(AgentSkillRepoCredential.agent_id)
    )
    return [AgentSkillRepoCredentialRow.model_validate(o) for o in result.scalars()]


async def get_tenant_skill_repo_proof_kind(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    repo_url: str,
) -> RepoProofKind | None:
    """Return a proof_kind THIS tenant established for `repo_url`, or None.

    The skill-repo counterpart of
    `agent_repo_binding.get_tenant_repo_proof_kind`, and needed for the same
    reason: GitHub App installation coverage is installed by repo owners for
    their own use, so it cannot stand in for this tenant having demonstrated
    read access. Returns the first non-null proof_kind among this tenant's own
    enrollments of that repo.
    """
    credentials = await list_skill_repo_credentials_for_repo(
        session, tenant_id=tenant_id, repo_url=repo_url
    )
    for credential in credentials:
        if credential.proof_kind is not None:
            return credential.proof_kind
    return None


async def delete_skill_repo_credential(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_url: str,
) -> None:
    """Remove the credential for (tenant, agent, repo). Idempotent — no raise if absent."""
    await session.execute(
        delete(AgentSkillRepoCredential).where(
            AgentSkillRepoCredential.tenant_id == tenant_id,
            AgentSkillRepoCredential.agent_id == agent_id,
            AgentSkillRepoCredential.repo_url == _normalize_owner_repo(repo_url),
        )
    )
    await session.flush()
