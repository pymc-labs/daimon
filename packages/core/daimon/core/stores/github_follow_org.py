"""An agent that follows its GitHub org holds every repo of the org, except those taken off it.

An admin turns `github_follow_org` on for one agent. On each refresh of an installation that
covers all of an org's repos, every following agent that already works in that installation is
given each listed repo it has no grant for and was never taken off (`AgentGitHubGrantRemoval`):
an agent-scoped workspace repo row where none exists, then the grant, written by the same
functions the connect page uses. Other agents are untouched.

Progress is the grants themselves, keyed on GitHub's repo id. No snapshot or baseline decides
what is new, so a repo another agent connected first, a failed earlier try, or a repo that was
skipped at the cap is given on the next refresh. Turning the feature on gives the gap at the
next refresh, as ticking "Select all" would. Each workspace is its own transaction: one that
fails does not stop the others, and each is safe to run again.

An agent holds at most one GitHub token's worth of repos in the org
(`REPOS_PER_INSTALLATION_TOKEN`), so a session keeps the one-token layout. The rest wait for
room, and the workspace's admins get an issue.

Renaming is not handled here: a renamed repo keeps its id, so a following agent that has it
keeps it, and whatever updates names on rename does so for every agent alike.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

import structlog
from daimon.core._models import (
    AgentGitHubGrant,
    AgentGitHubGrantRemoval,
    GitHubAppInstallation,
    TenantGitHubRepo,
)
from daimon.core.config import Settings
from daimon.core.features.snapshot import FeaturePlace, feature_in, feature_in_or_none
from daimon.core.github_app_auth import InstallationRepository
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import github_access
from daimon.core.stores.feature_values import list_agent_rows_with_value
from daimon.core.stores.github_app_installations import lock_for_reconciliation
from daimon.core.stores.github_connect import RepoConfirmation, write_repo_confirmation
from daimon.core.stores.tenants import get_tenant
from sqlalchemy import select
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
    limits: list[FollowLimit] = field(default_factory=list[FollowLimit])
    failed_tenants: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    """Workspaces whose step failed; the caller queues another refresh."""


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


async def follow_installation(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    installation_id: int,
    repos: Sequence[InstallationRepository],
    write_access: Callable[[], Awaitable[bool]],
    now: datetime,
) -> FollowOutcome:
    """Give each following agent the listed repos it lacks and was not taken off.

    `repos` is GitHub's complete current list. `write_access` asks whether the GitHub App
    holds the write profile on this installation (called only when an agent has a gap);
    without it the grant is read-only, as far as the App allows.
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
        ):
            return outcome
        followers = await _followers(session, installation_id=installation_id)
    listed = sorted(
        (repo for repo in repos if repo.repo_id is not None and repo.owner_id is not None),
        key=lambda repo: repo.repo_id or 0,
    )
    if not followers or not listed:
        return outcome
    writable: bool | None = None
    for tenant_id in sorted(followers, key=str):
        try:
            if writable is None:
                writable = await write_access()
            async with sessionmaker.begin() as session:
                await _follow_in_tenant(
                    session,
                    tenant_id=tenant_id,
                    followers=sorted(followers[tenant_id], key=lambda item: str(item.agent_id)),
                    installation_id=installation_id,
                    listed=listed,
                    write_access=writable,
                    now=now,
                    outcome=outcome,
                )
        except Exception:
            # Boundary: one workspace's failure must not stop the others; the caller queues
            # another refresh, and the step is safe to run again.
            log.exception(
                "github.follow_org.tenant_failed",
                tenant_id=str(tenant_id),
                installation_id=installation_id,
            )
            outcome.failed_tenants.append(tenant_id)
    return outcome


async def _held_in_installation(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, installation_id: int
) -> set[int]:
    """The distinct repo ids of this installation the agent holds a grant for."""
    return set(
        await session.scalars(
            select(AgentGitHubGrant.repo_id)
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
            .distinct()
        )
    )


async def _gaps(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    follower: _Follower,
    installation_id: int,
    listed: Sequence[InstallationRepository],
) -> tuple[list[InstallationRepository], bool]:
    """The listed repos this agent should be given now, and whether the cap cut some."""
    held = await _held_in_installation(
        session, tenant_id=tenant_id, agent_id=follower.agent_id, installation_id=installation_id
    )
    granted_any = set(
        await session.scalars(
            select(AgentGitHubGrant.repo_id).where(
                AgentGitHubGrant.tenant_id == tenant_id,
                AgentGitHubGrant.agent_id == follower.agent_id,
            )
        )
    )
    taken_off = set(
        await session.scalars(
            select(AgentGitHubGrantRemoval.repo_id).where(
                AgentGitHubGrantRemoval.tenant_id == tenant_id,
                AgentGitHubGrantRemoval.agent_id == follower.agent_id,
            )
        )
    )
    gap = [repo for repo in listed if repo.repo_id not in granted_any | taken_off]
    room = max(github_access.REPOS_PER_INSTALLATION_TOKEN - len(held), 0)
    return gap[:room], len(gap) > room


async def _follow_in_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    followers: Sequence[_Follower],
    installation_id: int,
    listed: Sequence[InstallationRepository],
    write_access: bool,
    now: datetime,
    outcome: FollowOutcome,
) -> None:
    gaps: dict[uuid.UUID, list[InstallationRepository]] = {}
    for follower in followers:
        gap, cut = await _gaps(
            session,
            tenant_id=tenant_id,
            follower=follower,
            installation_id=installation_id,
            listed=listed,
        )
        if cut:
            outcome.limits.append(FollowLimit(tenant_id, follower.ma_agent_id, "repos"))
        if gap:
            gaps[follower.agent_id] = gap
    if not gaps:
        return
    # Lock order: tenant, repository ids ascending, then the installation (as the connect
    # page's save takes them), then each agent ascending inside `stage_grant`.
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    await github_access.lock_repo_grants_batch(
        session,
        tenant_id=tenant_id,
        repo_ids={repo.repo_id for gap in gaps.values() for repo in gap if repo.repo_id},
    )
    installation = await lock_for_reconciliation(session, installation_id=installation_id)
    if installation is None:
        return
    access: Literal["read", "write"] = "write" if write_access else "read"
    covered = set(installation.repo_full_names)
    for follower in followers:
        for repo in gaps.get(follower.agent_id, ()):
            assert repo.repo_id is not None and repo.owner_id is not None
            # Another follower's step may have changed the facts since the gaps were read.
            if await session.get(
                AgentGitHubGrant, (tenant_id, follower.agent_id, repo.repo_id)
            ) or await session.get(
                AgentGitHubGrantRemoval, (tenant_id, follower.agent_id, repo.repo_id)
            ):
                continue
            existing = await github_access.repo_for_agent(
                session,
                tenant_id=tenant_id,
                repo_id=repo.repo_id,
                agent_id=follower.agent_id,
                for_update=True,
            )
            if existing is not None and existing.status != "active":
                continue  # revoked or suspended: an admin's call, not ours
            covered_before = set(covered)
            try:
                async with session.begin_nested():
                    if existing is None:
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
                    grant_access: Literal["read", "write"] = (
                        "write"
                        if access == "write"
                        and (existing is None or existing.max_access == "write")
                        else "read"
                    )
                    covered.add(repo.full_name)
                    installation.repo_full_names = sorted(covered)
                    await session.flush()
                    await github_access.stage_grant(
                        session,
                        tenant_id=tenant_id,
                        agent_id=follower.agent_id,
                        repo_id=repo.repo_id,
                        baseline_access=grant_access,
                        ceiling_access=grant_access,
                        granted_by_account_id=follower.set_by,
                        repo_lock_held=True,
                    )
            except ValueError as error:
                if str(error) == github_access.SERVER_WIDE_GRANT_MESSAGE:
                    # The admin who turned it on is no longer one: leave this repo, keep going.
                    log.warning("github.follow_org.granter_not_admin", tenant_id=str(tenant_id))
                    continue
                reason = _limit_reason(error)
                if reason is None:
                    raise
                covered = covered_before
                outcome.limits.append(FollowLimit(tenant_id, follower.ma_agent_id, reason))
                log.warning(
                    "github.follow_org.limit",
                    tenant_id=str(tenant_id),
                    installation_id=installation_id,
                    reason=reason,
                )
                break
            outcome.granted += 1
    installation.repo_full_names = sorted(covered)
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
