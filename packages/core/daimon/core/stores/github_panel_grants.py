"""Panel-specific GitHub grant drafts and PAT migration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, cast

from daimon.core._models import (
    AgentGitHubGrantDraft,
    GitHubAppInstallation,
)
from daimon.core.stores import github_access
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.github_connect import (
    require_app_eligible_agent,
)
from daimon.core.stores.github_installation_coverage import installation_covers_repo
from daimon.core.stores.security_audit import append_event
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

Access = Literal["none", "read", "write"]


@dataclass(frozen=True)
class RepoChoice:
    repo_id: int
    full_name: str
    max_access: Literal["read", "write"]
    baseline: Access | None
    ceiling: Literal["read", "write"] | None
    staged: bool
    working: bool
    live_baseline: Access | None = None
    live_ceiling: Literal["read", "write"] | None = None


@dataclass(frozen=True)
class GrantsPanel:
    repos: tuple[RepoChoice, ...]
    working_repo: str | None
    has_pending: bool = False

    def text(self, agent_name: str, *, page: int = 0, page_size: int = 20) -> str:
        lines = [f"Repos {agent_name} uses"]
        visible = [r for r in self.repos if r.live_ceiling is not None]
        for repo in visible[page * page_size : (page + 1) * page_size]:
            lines.append(f"• {repo.full_name}")
        if self.has_pending:
            lines.append("Changes waiting to be saved.")
        if len(visible) > page_size:
            lines.append(f"Page {page + 1} of {(len(visible) + page_size - 1) // page_size}")
        return "\n".join(lines)


async def load_grants_panel(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str | None = None,
) -> GrantsPanel:
    authorized = await github_access.list_authorized_repos(
        session, tenant_id=tenant_id, agent_id=agent_id
    )
    grants = {
        row.repo_id: row
        for row in await github_access.list_agent_grants(
            session, tenant_id=tenant_id, agent_id=agent_id
        )
    }
    drafts = {
        row.repo_id: row
        for row in await session.scalars(
            select(AgentGitHubGrantDraft).where(
                AgentGitHubGrantDraft.tenant_id == tenant_id,
                AgentGitHubGrantDraft.agent_id == agent_id,
            )
        )
    }
    policy = (
        await load_access_policy(session, tenant_id=tenant_id) if agent_name is not None else None
    )
    rule = policy.agent_rules.get(agent_name) if policy is not None and agent_name else None
    pinned = rule is not None and rule.runs_in is not None
    if pinned:
        authorized = [row for row in authorized if row.scope_agent_id == agent_id]
    working_repo = next(
        (
            row.repo_full_name
            for row in authorized
            if row.repo_id in grants and grants[row.repo_id].is_working_repo
        ),
        None,
    )
    repos = tuple(
        RepoChoice(
            repo_id=row.repo_id,
            full_name=row.repo_full_name,
            max_access=row.max_access,
            baseline=cast(
                Access | None,
                (
                    drafts[row.repo_id].baseline_access
                    if drafts[row.repo_id].operation == "upsert"
                    else None
                )
                if row.repo_id in drafts
                else (grants[row.repo_id].baseline_access if row.repo_id in grants else None),
            ),
            ceiling=cast(
                Literal["read", "write"] | None,
                (
                    drafts[row.repo_id].ceiling_access
                    if drafts[row.repo_id].operation == "upsert"
                    else None
                )
                if row.repo_id in drafts
                else (grants[row.repo_id].ceiling_access if row.repo_id in grants else None),
            ),
            staged=row.repo_id in drafts,
            working=(
                drafts[row.repo_id].is_working_repo
                if drafts[row.repo_id].operation == "upsert"
                else False
            )
            if row.repo_id in drafts
            else (grants[row.repo_id].is_working_repo if row.repo_id in grants else False),
            live_baseline=(grants[row.repo_id].baseline_access if row.repo_id in grants else None),
            live_ceiling=(grants[row.repo_id].ceiling_access if row.repo_id in grants else None),
        )
        for row in authorized
        if row.status == "active"
    )
    return GrantsPanel(
        repos=repos,
        working_repo=working_repo,
        has_pending=bool(drafts),
    )


async def stage_panel_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    baseline_access: Access,
    ceiling_access: Literal["read", "write"],
    account_id: uuid.UUID | None,
    is_working_repo: bool,
) -> None:
    """Keep an active agent's changes separate from its current live grants."""
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    baseline_access = "write"
    ceiling_access = "write"
    authorized = await github_access.repo_for_agent(
        session, tenant_id=tenant_id, repo_id=repo_id, agent_id=agent_id, for_update=True
    )
    if authorized is None or authorized.status != "active":
        raise ValueError("Repository is not connected here.")
    await github_access.require_may_grant(session, repo=authorized, account_id=account_id)
    installation = await session.get(GitHubAppInstallation, authorized.installation_id)
    if not installation_covers_repo(installation, authorized.repo_full_name):
        raise ValueError("GitHub installation is unavailable.")
    rank = {"none": 0, "read": 1, "write": 2}
    if (
        rank[baseline_access] > rank[ceiling_access]
        or rank[ceiling_access] > rank[authorized.max_access]
    ):
        raise ValueError("Access exceeds the repo's confirmed limit.")
    row = await session.get(AgentGitHubGrantDraft, (tenant_id, agent_id, repo_id))
    if row is None:
        row = AgentGitHubGrantDraft(tenant_id=tenant_id, agent_id=agent_id, repo_id=repo_id)
        session.add(row)
    row.operation = "upsert"
    row.baseline_access = baseline_access
    row.ceiling_access = ceiling_access
    row.is_working_repo = is_working_repo
    row.granted_by_account_id = account_id
    await session.flush()
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="github_grants",
        operation="github_grant",
        outcome="allowed",
        reason="grant staged",
        github_repo_ids=[repo_id],
    )


async def remove_panel_grant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    repo_id: int,
    account_id: uuid.UUID | None,
) -> None:
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    row = await session.get(AgentGitHubGrantDraft, (tenant_id, agent_id, repo_id))
    if row is None:
        row = AgentGitHubGrantDraft(tenant_id=tenant_id, agent_id=agent_id, repo_id=repo_id)
        session.add(row)
    row.operation = "remove"
    row.baseline_access = None
    row.ceiling_access = None
    row.is_working_repo = False
    row.granted_by_account_id = account_id
    await session.flush()
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=agent_id,
        platform=None,
        platform_user_id=None,
        tool_name="github_grants",
        operation="github_grant",
        outcome="allowed",
        reason="grant removal staged",
        github_repo_ids=[repo_id],
    )


async def activate_grants(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID | None,
    agent_name: str,
) -> bool:
    """Apply the panel's draft changes to live App grants."""
    await github_access.lock_tenant_grants(session, tenant_id=tenant_id)
    await require_app_eligible_agent(
        session, tenant_id=tenant_id, agent_id=agent_id, agent_name=agent_name
    )
    drafts = list(
        await session.scalars(
            select(AgentGitHubGrantDraft)
            .where(
                AgentGitHubGrantDraft.tenant_id == tenant_id,
                AgentGitHubGrantDraft.agent_id == agent_id,
            )
            .with_for_update()
        )
    )
    await github_access.lock_repo_grants_batch(
        session, tenant_id=tenant_id, repo_ids={draft.repo_id for draft in drafts}
    )
    for draft in sorted(drafts, key=lambda item: item.repo_id):
        if draft.operation == "remove":
            await github_access.remove_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=draft.repo_id,
                changed_by_account_id=account_id,
            )
        else:
            if draft.baseline_access not in (
                "none",
                "read",
                "write",
            ) or draft.ceiling_access not in ("read", "write"):
                raise ValueError("Staged access is invalid.")
            await github_access.stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=draft.repo_id,
                baseline_access="write",
                ceiling_access="write",
                granted_by_account_id=account_id,
                is_working_repo=draft.is_working_repo,
                agent_name=agent_name,
                repo_lock_held=True,
            )
    if drafts:
        await session.execute(
            delete(AgentGitHubGrantDraft).where(
                AgentGitHubGrantDraft.tenant_id == tenant_id,
                AgentGitHubGrantDraft.agent_id == agent_id,
            )
        )
    return bool(drafts)
