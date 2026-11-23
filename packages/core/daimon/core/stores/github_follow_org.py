"""An agent that follows its GitHub org gets each repo added to the org later.

An admin turns `github_follow_org` on for one agent. When the refresh of an installation
that covers all of an org's repos finds a repo the last snapshot did not list, each
following agent that already works in that installation gets the repo: a workspace repo
row scoped to that agent, then the grant, written by the same functions the connect page
uses. Other agents, and agents that do not follow, are untouched.

An agent never follows past one GitHub token's worth of repos in the org
(`REPOS_PER_INSTALLATION_TOKEN`): a session then keeps the one-token layout. Past that, or
past any other grant limit, nothing more is given and the workspace's admins get an issue.

The step runs before the refresh stores its snapshot, so the stored snapshot is still the
"before" and a failed step is retried with the same difference. It is idempotent: a repo
the agent already has a row for (granted, or taken off on purpose) is never granted again.
Repos listed when the feature was turned on are in the snapshot, so they stay as they are.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

import structlog
from daimon.core._models import AgentGitHubGrant, GitHubAppInstallation, TenantGitHubRepo
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, feature_in, feature_in_or_none
from daimon.core.github_app_auth import InstallationRepository
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import github_access
from daimon.core.stores.feature_values import list_agent_rows_with_value
from daimon.core.stores.github_app_installations import (
    StaleInstallationSnapshotError,
    lock_for_reconciliation,
)
from daimon.core.stores.github_connect import RepoConfirmation, write_repo_confirmation
from daimon.core.stores.tenants import get_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

FEATURE_KEY: Final = "github_follow_org"
LimitReason = Literal["repos", "accounts", "credentials", "read_only"]


@dataclass(frozen=True)
class _Follower:
    tenant_id: uuid.UUID
    ma_agent_id: str
    agent_id: uuid.UUID
    set_by: uuid.UUID
    github_user_id: int


@dataclass(frozen=True)
class FollowLimit:
    """An agent that could not take a repo: its GitHub limit is reached."""

    tenant_id: uuid.UUID
    ma_agent_id: str
    reason: LimitReason


@dataclass
class FollowOutcome:
    granted: int = 0
    renamed: int = 0
    limits: list[FollowLimit] = field(default_factory=list[FollowLimit])


def _limit_reason(error: ValueError) -> LimitReason | None:
    if isinstance(error, github_access.InstallationLimitError):
        return "accounts"
    if isinstance(error, github_access.SessionVaultBudgetError):
        return "credentials"
    if isinstance(error, github_access.ReadOnlyMultiInstallationError):
        return "read_only"
    return None


async def _followers(
    session: AsyncSession, *, installation_id: int
) -> dict[uuid.UUID, list[_Follower]]:
    """Agents with the feature on that already hold a repo of this installation.

    The held repo supplies who confirmed the org on the agent's behalf (a GitHub user the
    new rows name); an agent with none has no standing here and is skipped.
    """
    tenants = set(
        await session.scalars(
            select(TenantGitHubRepo.tenant_id).where(
                TenantGitHubRepo.installation_id == installation_id,
                TenantGitHubRepo.status == "active",
            )
        )
    )
    if not tenants:
        return {}
    found: dict[uuid.UUID, list[_Follower]] = {}
    for row in await list_agent_rows_with_value(
        session, key=FEATURE_KEY, value="on", tenant_ids=tenants
    ):
        if row.set_by is None:
            continue
        tenant = await get_tenant(session, row.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            continue
        place = FeaturePlace(row.tenant_id, tenant.platform, agent_id=row.agent_id)
        resolved = await feature_in_or_none(session, None, place, "github_follow_org")
        if resolved is None or not resolved.on:
            continue
        agent_id = derive_agent_uuid(tenant_id=row.tenant_id, ma_agent_id=row.agent_id)
        basis = await session.scalar(
            select(TenantGitHubRepo)
            .join(
                AgentGitHubGrant,
                (AgentGitHubGrant.tenant_id == TenantGitHubRepo.tenant_id)
                & (AgentGitHubGrant.repo_id == TenantGitHubRepo.repo_id),
            )
            .where(
                TenantGitHubRepo.tenant_id == row.tenant_id,
                TenantGitHubRepo.installation_id == installation_id,
                TenantGitHubRepo.status == "active",
                AgentGitHubGrant.agent_id == agent_id,
                AgentGitHubGrant.app_visible.is_(True),
                (TenantGitHubRepo.scope_agent_id.is_(None))
                | (TenantGitHubRepo.scope_agent_id == agent_id),
            )
            .order_by(TenantGitHubRepo.authorized_at.desc())
            .limit(1)
        )
        if basis is None:
            continue
        found.setdefault(row.tenant_id, []).append(
            _Follower(
                tenant_id=row.tenant_id,
                ma_agent_id=row.agent_id,
                agent_id=agent_id,
                set_by=row.set_by,
                github_user_id=basis.authorized_by_github_user_id,
            )
        )
    return found


async def follow_new_repos(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    installation_id: int,
    repos: Sequence[InstallationRepository],
    write_access: Callable[[], Awaitable[bool]],
    now: datetime,
) -> FollowOutcome:
    """Give each following agent the repos GitHub lists now and the stored snapshot lacks.

    `write_access` asks whether the GitHub App holds the write profile on this installation
    (called only when an agent will get a repo); without it the grant is read-only, as far
    as the App allows. Raises
    `StaleInstallationSnapshotError` when another writer refreshed the installation
    meanwhile; the caller's job is then retried.
    """
    outcome = FollowOutcome()
    async with sessionmaker() as session:
        installation = await session.get(GitHubAppInstallation, installation_id)
        if (
            installation is None
            or installation.app != "github_app"
            or installation.repository_selection != "all"
            or installation.suspended_at is not None
            or installation.github_removed_at is not None
            or installation.github_repo_full_names is None
        ):
            return outcome
        known = set(installation.github_repo_full_names)
        observed_refresh = installation.github_refreshed_at
        new = sorted(
            (
                repo
                for repo in repos
                if repo.full_name not in known
                and repo.repo_id is not None
                and repo.owner_id is not None
            ),
            key=lambda repo: repo.repo_id or 0,
        )
        if not new:
            return outcome
        followers = await _followers(session, installation_id=installation_id)
    if not followers:
        return outcome
    writable = await write_access()
    for tenant_id in sorted(followers, key=str):
        async with sessionmaker.begin() as session:
            await _follow_in_tenant(
                session,
                tenant_id=tenant_id,
                followers=sorted(followers[tenant_id], key=lambda item: str(item.agent_id)),
                installation_id=installation_id,
                observed_refresh=observed_refresh,
                new=new,
                write_access=writable,
                now=now,
                outcome=outcome,
            )
    return outcome


async def _held_in_installation(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, installation_id: int
) -> int:
    """How many repos of this installation the agent holds a grant for."""
    return (
        await session.scalar(
            select(func.count())
            .select_from(AgentGitHubGrant)
            .join(
                TenantGitHubRepo,
                (TenantGitHubRepo.tenant_id == AgentGitHubGrant.tenant_id)
                & (TenantGitHubRepo.repo_id == AgentGitHubGrant.repo_id),
            )
            .where(
                AgentGitHubGrant.tenant_id == tenant_id,
                AgentGitHubGrant.agent_id == agent_id,
                AgentGitHubGrant.app_visible.is_(True),
                TenantGitHubRepo.installation_id == installation_id,
                (TenantGitHubRepo.scope_agent_id.is_(None))
                | (TenantGitHubRepo.scope_agent_id == agent_id),
            )
        )
        or 0
    )


async def _follow_in_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    followers: Sequence[_Follower],
    installation_id: int,
    observed_refresh: datetime | None,
    new: Sequence[InstallationRepository],
    write_access: bool,
    now: datetime,
    outcome: FollowOutcome,
) -> None:
    # Lock order: tenant, repository ids ascending, then the installation (as the connect
    # page's save takes them), then each agent ascending inside `stage_grant`.
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    await github_access.lock_repo_grants_batch(
        session, tenant_id=tenant_id, repo_ids={repo.repo_id for repo in new if repo.repo_id}
    )
    installation = await lock_for_reconciliation(session, installation_id=installation_id)
    if (
        installation is None
        or installation.github_repo_full_names is None
        or installation.github_refreshed_at != observed_refresh
    ):
        raise StaleInstallationSnapshotError(
            "GitHub installation snapshot changed while new repos were followed"
        )
    access: Literal["read", "write"] = "write" if write_access else "read"
    covered = set(installation.repo_full_names)
    listed = set(installation.github_repo_full_names)
    for follower in followers:
        held = await _held_in_installation(
            session,
            tenant_id=tenant_id,
            agent_id=follower.agent_id,
            installation_id=installation_id,
        )
        for repo in new:
            assert repo.repo_id is not None and repo.owner_id is not None
            existing = await github_access.repo_for_agent(
                session,
                tenant_id=tenant_id,
                repo_id=repo.repo_id,
                agent_id=follower.agent_id,
                for_update=True,
            )
            if existing is not None:
                # Seen before: granted already, taken off on purpose, or renamed.
                if (
                    existing.scope_agent_id == follower.agent_id
                    and existing.status == "active"
                    and existing.repo_full_name != repo.full_name
                ):
                    existing.repo_full_name = repo.full_name
                    existing.version += 1
                    covered.add(repo.full_name)
                    listed.add(repo.full_name)
                    outcome.renamed += 1
                continue
            if held >= github_access.REPOS_PER_INSTALLATION_TOKEN:
                outcome.limits.append(FollowLimit(tenant_id, follower.ma_agent_id, "repos"))
                break
            covered_before, listed_before = set(covered), set(listed)
            try:
                async with session.begin_nested():
                    await write_repo_confirmation(
                        session,
                        tenant_id=tenant_id,
                        scope_agent_id=follower.agent_id,
                        repo=RepoConfirmation(
                            repo_id=repo.repo_id,
                            owner_id=repo.owner_id,
                            installation_id=installation_id,
                            full_name=repo.full_name,
                            max_access=access,
                            authorizer_permission=None,
                        ),
                        github_user_id=follower.github_user_id,
                        account_id=follower.set_by,
                        max_access=access,
                        now=now,
                    )
                    covered.add(repo.full_name)
                    listed.add(repo.full_name)
                    installation.repo_full_names = sorted(covered)
                    installation.github_repo_full_names = sorted(listed)
                    await session.flush()
                    await github_access.stage_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=follower.agent_id,
                        repo_id=repo.repo_id,
                        baseline_access=access,
                        ceiling_access=access,
                        granted_by_account_id=follower.set_by,
                        repo_lock_held=True,
                    )
            except ValueError as error:
                reason = _limit_reason(error)
                if reason is None:
                    raise
                covered, listed = covered_before, listed_before
                outcome.limits.append(FollowLimit(tenant_id, follower.ma_agent_id, reason))
                log.warning(
                    "github.follow_org.limit",
                    tenant_id=str(tenant_id),
                    installation_id=installation_id,
                    reason=reason,
                )
                break
            held += 1
            outcome.granted += 1
    installation.repo_full_names = sorted(covered)
    installation.github_repo_full_names = sorted(listed)
    await session.flush()


async def agent_follows(
    session: AsyncSession,
    settings: Settings | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    ma_agent_id: str,
) -> bool:
    """Whether `github_follow_org` is on for this agent, resolved as any reader does."""
    place = FeaturePlace(tenant_id, platform, agent_id=ma_agent_id)
    return (await feature_in(session, settings, place, "github_follow_org")).on
