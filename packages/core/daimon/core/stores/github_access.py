"""Tenant-scoped GitHub authorization records. Callers own transactions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, cast

from daimon.core._models import (
    Account,
    AgentGitHubGrant,
    AgentGitHubGrantDraft,
    GitHubAppInstallation,
    TenantGitHubRepo,
)
from daimon.core.stores.github_installation_coverage import installation_covers_repo
from daimon.core.stores.github_owner_grants import list_owner_grants
from daimon.core.stores.security_audit import append_event
from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession


class AuthorizedRepo(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    repo_id: int
    scope_agent_id: uuid.UUID | None = None
    owner_id: int
    installation_id: int
    repo_full_name: str
    max_access: Literal["read", "write"]
    authorized_by_github_user_id: int
    authorized_by_account_id: uuid.UUID | None
    authorized_permission: str | None = None
    authorized_at: datetime
    status: Literal["active", "suspended", "revoked"]
    status_reason: str | None
    version: int


class AgentGrant(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    repo_id: int
    baseline_access: Literal["none", "read", "write"]
    ceiling_access: Literal["read", "write"]
    mount_path: str | None
    is_working_repo: bool
    granted_by_account_id: uuid.UUID | None
    granted_at: datetime
    version: int


class SecondInstallationError(ValueError):
    """A grant would make one agent use two GitHub installations.

    `org` is the owner login of the installation the agent already uses. `kind`
    is what blocks the new grant: explicit repo grants (`repo`) or an owner-wide
    grant (`owner`), which differ in what has to be removed to switch.
    """

    def __init__(
        self,
        message: str,
        *,
        org: str | None = None,
        kind: Literal["repo", "owner"] = "repo",
    ) -> None:
        super().__init__(message)
        self.org = org
        self.kind: Literal["repo", "owner"] = kind


# A concurrent grant writer won: the transaction restarts from fresh state.
# 40001 is a serialization failure and 40P01 a deadlock.
_CONCURRENT_CHANGE_SQLSTATES = frozenset({"40001", "40P01"})


def is_concurrent_grant_change(exc: DBAPIError) -> bool:
    """True for a serialization failure or deadlock, which a retry resolves."""
    return getattr(exc.orig, "sqlstate", None) in _CONCURRENT_CHANGE_SQLSTATES


async def lock_agent_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> None:
    """Serialize the agent's grant writers by bumping its fence row.

    Every grant writer upserts the same row, so a concurrent writer waits for
    this transaction. Under READ COMMITTED the waiter then reads fresh state.
    Under SERIALIZABLE, updating a row another transaction committed after the
    waiter's snapshot raises 40001, so a stale snapshot never carries on to
    write. An advisory lock gave neither: it left the snapshot in place. Take
    an installation lock before this one, so every writer orders the two alike.
    """
    await session.execute(
        text(
            "INSERT INTO agent_github_grant_fences (tenant_id, agent_id, version, updated_at) "
            "VALUES (:tenant_id, :agent_id, 1, now()) "
            "ON CONFLICT (tenant_id, agent_id) DO UPDATE "
            "SET version = agent_github_grant_fences.version + 1, updated_at = now()"
        ),
        {"tenant_id": tenant_id, "agent_id": agent_id},
    )


def _visible_to(agent_id: uuid.UUID | None) -> ColumnElement[bool]:
    """Server-wide rows, plus the agent's own rows when an agent is named."""
    if agent_id is None:
        return TenantGitHubRepo.scope_agent_id.is_(None)
    return or_(
        TenantGitHubRepo.scope_agent_id.is_(None), TenantGitHubRepo.scope_agent_id == agent_id
    )


async def repo_for_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    repo_id: int,
    agent_id: uuid.UUID | None,
    for_update: bool = False,
) -> TenantGitHubRepo | None:
    """The confirmation `agent_id` may use for `repo_id`: its own row, else the server-wide one.

    A row owned by another agent is never returned. `agent_id=None` reads only
    the server-wide row.
    """
    query = (
        select(TenantGitHubRepo)
        .where(
            TenantGitHubRepo.tenant_id == tenant_id,
            TenantGitHubRepo.repo_id == repo_id,
            _visible_to(agent_id),
        )
        # False sorts first: the agent's own row wins over the server-wide one.
        .order_by(TenantGitHubRepo.scope_agent_id.is_(None))
        .limit(1)
    )
    if for_update:
        query = query.with_for_update()
    return await session.scalar(query)


async def list_authorized_repos(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID | None = None
) -> list[AuthorizedRepo]:
    """Server-wide repos, or with `agent_id` the ones that agent may use, one row per repo."""
    rows = await session.scalars(
        select(TenantGitHubRepo)
        .where(TenantGitHubRepo.tenant_id == tenant_id, _visible_to(agent_id))
        .order_by(TenantGitHubRepo.repo_id, TenantGitHubRepo.scope_agent_id.is_(None))
    )
    picked: dict[int, AuthorizedRepo] = {}
    for row in rows:
        picked.setdefault(row.repo_id, AuthorizedRepo.model_validate(row))
    return list(picked.values())


async def upsert_linked_agent_authorization(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    owner_id: int,
    installation_id: int,
    repo_full_name: str,
    access: Literal["read", "write"],
    github_user_id: int,
    account_id: uuid.UUID,
    permission: str,
) -> None:
    """Record the linked person's grant-time authority for one agent repo."""
    row = await session.scalar(
        select(TenantGitHubRepo)
        .where(
            TenantGitHubRepo.tenant_id == tenant_id,
            TenantGitHubRepo.repo_id == repo_id,
            TenantGitHubRepo.scope_agent_id == agent_id,
        )
        .with_for_update()
    )
    if row is None:
        row = TenantGitHubRepo(tenant_id=tenant_id, repo_id=repo_id, scope_agent_id=agent_id)
        session.add(row)
    elif row.status != "active":
        row.version += 1
    row.owner_id = owner_id
    row.installation_id = installation_id
    row.repo_full_name = repo_full_name
    row.max_access = access
    row.authorized_by_github_user_id = github_user_id
    row.authorized_by_account_id = account_id
    row.authorized_permission = permission
    row.status = "active"
    row.status_reason = None
    await session.flush()


async def list_agent_grants(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[AgentGrant]:
    rows = await session.scalars(
        select(AgentGitHubGrant).where(
            AgentGitHubGrant.tenant_id == tenant_id,
            AgentGitHubGrant.agent_id == agent_id,
            AgentGitHubGrant.app_visible.is_(True),
        )
    )
    return [AgentGrant.model_validate(row) for row in rows]


async def list_live_grant_repositories(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[tuple[AgentGrant, AuthorizedRepo]]:
    """Only live grants with still-active tenant authorization and installation."""
    grants = await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    result: list[tuple[AgentGrant, AuthorizedRepo]] = []
    for grant in grants:
        row = await repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
        if row is None or row.status != "active":
            continue
        installation = await session.get(GitHubAppInstallation, row.installation_id)
        if not installation_covers_repo(installation, row.repo_full_name):
            continue
        result.append((grant, AuthorizedRepo.model_validate(row)))
    return result


async def active_grant_for_repo(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, repo_name: str
) -> AuthorizedRepo | None:
    """Return an agent's usable App grant for one repository."""
    for _, repo in await list_live_grant_repositories(
        session, tenant_id=tenant_id, agent_id=agent_id
    ):
        if repo.repo_full_name.casefold() == repo_name.casefold():
            return repo
    return None


class AgentRepo(BaseModel):
    """One repo an agent has, with the confirmation it rests on."""

    model_config = ConfigDict(frozen=True)
    repo_id: int
    full_name: str
    # "agent": connected for this agent only. "shared": a server-wide repo.
    scope: Literal["agent", "shared"]
    max_access: Literal["read", "write"]
    baseline_access: Literal["none", "read", "write"]
    ceiling_access: Literal["read", "write"]
    status: Literal["active", "suspended", "revoked"]
    added_by_account_id: uuid.UUID | None
    added_at: datetime
    granted_by_account_id: uuid.UUID | None
    granted_at: datetime
    is_working_repo: bool = False
    grant_version: int = 0
    authorization_version: int = 0


async def list_agent_repos(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> list[AgentRepo]:
    """The agent's granted repos, each read from the row it may use."""
    result: list[AgentRepo] = []
    for grant in await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id):
        row = await repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
        if row is None:
            continue
        result.append(
            AgentRepo(
                repo_id=row.repo_id,
                full_name=row.repo_full_name,
                scope="shared" if row.scope_agent_id is None else "agent",
                max_access=cast(Literal["read", "write"], row.max_access),
                baseline_access=grant.baseline_access,
                ceiling_access=grant.ceiling_access,
                status=cast(Literal["active", "suspended", "revoked"], row.status),
                added_by_account_id=row.authorized_by_account_id,
                added_at=row.authorized_at,
                granted_by_account_id=grant.granted_by_account_id,
                granted_at=grant.granted_at,
                is_working_repo=grant.is_working_repo,
                grant_version=grant.version,
                authorization_version=row.version,
            )
        )
    return sorted(result, key=lambda repo: repo.full_name.casefold())


async def repo_id_for_agent_removal(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, repo_name: str
) -> int | None:
    """Find a repo on this agent's grant or pending draft, including a revoked draft."""
    for repo in await list_agent_repos(session, tenant_id=tenant_id, agent_id=agent_id):
        if repo.full_name.casefold() == repo_name.casefold():
            return repo.repo_id
    drafts = await session.scalars(
        select(AgentGitHubGrantDraft).where(
            AgentGitHubGrantDraft.tenant_id == tenant_id,
            AgentGitHubGrantDraft.agent_id == agent_id,
        )
    )
    for draft in drafts:
        repo = await repo_for_agent(
            session, tenant_id=tenant_id, repo_id=draft.repo_id, agent_id=agent_id
        )
        if repo is not None and repo.repo_full_name.casefold() == repo_name.casefold():
            return draft.repo_id
    return None


async def set_working_repo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_name: str | None,
    account_id: uuid.UUID,
) -> str | None:
    """Select one live grant as the filesystem repo, or clear the selection.

    The caller verifies that the account manages this agent. Locking the
    agent's grant rows serializes changes to an existing grant set.
    """
    rows = list(
        await session.scalars(
            select(AgentGitHubGrant)
            .where(
                AgentGitHubGrant.tenant_id == tenant_id,
                AgentGitHubGrant.agent_id == agent_id,
                AgentGitHubGrant.app_visible.is_(True),
            )
            .order_by(AgentGitHubGrant.repo_id)
            .with_for_update()
        )
    )
    selected: AgentGitHubGrant | None = None
    selected_name: str | None = None
    if repo_name is not None:
        for grant in rows:
            repo = await repo_for_agent(
                session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
            )
            if repo is None or repo.status != "active":
                continue
            installation = await session.get(GitHubAppInstallation, repo.installation_id)
            if not installation_covers_repo(installation, repo.repo_full_name):
                continue
            if repo.repo_full_name.casefold() == repo_name.casefold():
                selected, selected_name = grant, repo.repo_full_name
                break
        if selected is None:
            raise ValueError("That repo is not on this agent's list. Offer Connect GitHub.")
    changed_ids: list[int] = []
    for grant in rows:
        wanted = grant is selected
        if grant.is_working_repo != wanted:
            grant.is_working_repo = wanted
            grant.version += 1
            changed_ids.append(grant.repo_id)
    await session.flush()
    if changed_ids:
        await append_event(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            platform=None,
            platform_user_id=None,
            tool_name="set_working_repo",
            operation="github_grant",
            outcome="allowed",
            reason="working repo selected" if selected else "working repo cleared",
            github_repo_ids=changed_ids,
        )
    return selected_name


async def remove_agent_repo(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    account_id: uuid.UUID | None,
) -> bool:
    """Take a repo away from one agent now. The caller checks they manage the agent.

    Removes the grant and any pending draft. A repo connected for this agent
    only is disconnected too, since no other agent may use it; adding it back
    needs a new Connect. A server-wide repo stays connected for other agents.
    """
    await lock_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    removed = await remove_grant(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        repo_id=repo_id,
        changed_by_account_id=account_id,
    )
    draft = await session.get(AgentGitHubGrantDraft, (tenant_id, agent_id, repo_id))
    if draft is not None:
        await session.delete(draft)
        removed = True
    own = await session.scalar(
        select(TenantGitHubRepo)
        .where(
            TenantGitHubRepo.tenant_id == tenant_id,
            TenantGitHubRepo.repo_id == repo_id,
            TenantGitHubRepo.scope_agent_id == agent_id,
            TenantGitHubRepo.status == "active",
        )
        .with_for_update()
    )
    if own is not None:
        own.status = "revoked"
        own.status_reason = "removed from the agent in Daimon"
        own.version += 1
        removed = True
        await append_event(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            agent_id=agent_id,
            platform=None,
            platform_user_id=None,
            tool_name="github_grants",
            operation="github_connect",
            outcome="allowed",
            reason="agent repo disconnected",
            github_repo_ids=[repo_id],
        )
    await session.flush()
    return removed


_ACCESS_RANK = {"none": 0, "read": 1, "write": 2}

SERVER_WIDE_GRANT_MESSAGE = "Only a server admin can add a repo connected for the whole server."


async def require_may_grant(
    session: AsyncSession, *, repo: TenantGitHubRepo, account_id: uuid.UUID | None
) -> None:
    """A server-wide repo is a server admin's to grant; an agent's own repo, its manager's.

    `account_id=None` is the operator CLI. Whether the account manages the
    agent at all is the caller's check.
    """
    if repo.scope_agent_id is not None or account_id is None:
        return
    account = await session.get(Account, account_id)
    if account is None or account.role != "admin" or account.is_external:
        raise ValueError(SERVER_WIDE_GRANT_MESSAGE)


def other_installation_message(
    agent_name: str, org: str | None, kind: Literal["repo", "owner"] = "repo"
) -> str:
    """The person-facing text for a refused second installation.

    `kind` is what blocks: an owner-wide grant needs all the org's repos removed.
    The store raises `SecondInstallationError` with this text, so every surface
    that shows the exception (the browser confirmation, the tools) says it.
    """
    if org is None:
        return (
            f"{agent_name} already has repos from another GitHub account or org.\n"
            "An agent uses one GitHub account or org at a time. "
            "To switch, remove those repos first."
        )
    scope = f"all {org} repos" if kind == "owner" else f"the {org} repos"
    return (
        f"{agent_name} already has repos from {org}.\n"
        f"An agent uses one GitHub account or org at a time. To switch, remove {scope} first."
    )


def _second_installation(
    agent_name: str | None, agent_id: uuid.UUID, org: str, kind: Literal["repo", "owner"]
) -> SecondInstallationError:
    return SecondInstallationError(
        other_installation_message(agent_name or str(agent_id), org, kind), org=org, kind=kind
    )


async def _refuse_other_owner_installation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    installation_ids: set[int],
    agent_name: str | None,
) -> None:
    """Refuse when an owner grant of the agent sits on an installation outside the set."""
    for owner_grant in await list_owner_grants(session, tenant_id=tenant_id, agent_id=agent_id):
        if owner_grant.installation_id not in installation_ids:
            raise _second_installation(agent_name, agent_id, owner_grant.owner_login, "owner")


async def require_owner_grant_installations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    installation_ids: set[int],
    agent_name: str | None = None,
) -> None:
    """An owner grant keeps the one-installation rule `stage_grant` keeps for repos.

    Bumps the agent's grant fence (`lock_agent_grants`), so owner and explicit grants serialize per
    agent, then refuses when the agent has an explicit grant or an owner grant
    on an installation outside `installation_ids`. Re-adding an owner grant for the installation
    the agent already uses passes. The caller owns the transaction.
    """
    await lock_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    for grant in await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id):
        other = await repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
        if other is not None and other.installation_id not in installation_ids:
            raise _second_installation(
                agent_name, agent_id, other.repo_full_name.split("/", 1)[0], "repo"
            )
    await _refuse_other_owner_installation(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        installation_ids=installation_ids,
        agent_name=agent_name,
    )


async def stage_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    baseline_access: Literal["none", "read", "write"],
    ceiling_access: Literal["read", "write"],
    granted_by_account_id: uuid.UUID | None,
    mount_path: str | None = None,
    is_working_repo: bool = False,
    agent_name: str | None = None,
) -> AgentGrant:
    """Stage or update a grant. The caller owns the transaction and authorizes the actor."""
    if _ACCESS_RANK[baseline_access] > _ACCESS_RANK[ceiling_access]:
        raise ValueError("baseline access exceeds ceiling access")
    if mount_path is not None and (
        not mount_path.startswith("/workspace/") or ".." in mount_path.split("/")
    ):
        raise ValueError("mount path must be inside /workspace")
    await lock_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    authorized = await repo_for_agent(
        session, tenant_id=tenant_id, repo_id=repo_id, agent_id=agent_id, for_update=True
    )
    if authorized is None or authorized.status != "active":
        raise ValueError("repository is not actively authorized for this workspace")
    await require_may_grant(session, repo=authorized, account_id=granted_by_account_id)
    installation = await session.get(GitHubAppInstallation, authorized.installation_id)
    if not installation_covers_repo(installation, authorized.repo_full_name):
        raise ValueError("GitHub App installation is not active")
    for grant in await list_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id):
        if grant.repo_id == repo_id:
            continue
        other = await repo_for_agent(
            session, tenant_id=tenant_id, repo_id=grant.repo_id, agent_id=agent_id
        )
        if other is not None and other.installation_id != authorized.installation_id:
            raise _second_installation(
                agent_name, agent_id, other.repo_full_name.split("/", 1)[0], "repo"
            )
    await _refuse_other_owner_installation(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        installation_ids={authorized.installation_id},
        agent_name=agent_name,
    )
    if is_working_repo:
        working = await session.scalars(
            select(AgentGitHubGrant)
            .where(
                AgentGitHubGrant.tenant_id == tenant_id,
                AgentGitHubGrant.agent_id == agent_id,
                AgentGitHubGrant.app_visible.is_(True),
                AgentGitHubGrant.is_working_repo.is_(True),
                AgentGitHubGrant.repo_id != repo_id,
            )
            .with_for_update()
        )
        for previous in working:
            previous.is_working_repo = False
            previous.version += 1
    row = await session.get(AgentGitHubGrant, (tenant_id, agent_id, repo_id), with_for_update=True)
    if row is None:
        row = AgentGitHubGrant(
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            version=1,
            granted_at=datetime.now().astimezone(),
        )
        session.add(row)
    elif (
        row.app_visible
        and row.baseline_access == baseline_access
        and row.ceiling_access == ceiling_access
        and row.mount_path == mount_path
        and row.is_working_repo == is_working_repo
    ):
        return AgentGrant.model_validate(row)
    else:
        row.version += 1
    row.baseline_access = baseline_access
    row.ceiling_access = ceiling_access
    row.app_visible = True
    row.mount_path = mount_path
    row.is_working_repo = is_working_repo
    row.granted_by_account_id = granted_by_account_id
    await session.flush()
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=granted_by_account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="github_grants",
        operation="github_grant",
        outcome="allowed",
        reason="grant updated",
        github_repo_ids=[repo_id],
    )
    return AgentGrant.model_validate(row)


async def remove_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    changed_by_account_id: uuid.UUID | None = None,
) -> bool:
    await lock_agent_grants(session, tenant_id=tenant_id, agent_id=agent_id)
    row = await session.get(AgentGitHubGrant, (tenant_id, agent_id, repo_id), with_for_update=True)
    if row is None or not row.app_visible:
        return False
    await session.delete(row)
    await session.flush()
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=changed_by_account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="github_grants",
        operation="github_grant",
        outcome="allowed",
        reason="grant removed",
        github_repo_ids=[repo_id],
    )
    return True
