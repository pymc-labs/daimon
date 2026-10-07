"""Panel-specific GitHub grant drafts and PAT migration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, cast

from daimon.core._models import (
    AgentGitHubGrantDraft,
    AgentSkillRepoCredential,
    GitHubAppInstallation,
    TenantGitHubRepo,
)
from daimon.core.stores import agent_files, agent_github_binding, agent_repo_binding, github_access
from daimon.core.stores.github_credentials import delete_credential_for_principal
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
    mode: Literal["legacy", "app"]
    repos: tuple[RepoChoice, ...]
    working_repo: str | None
    has_pat: bool
    has_pending: bool = False

    def text(self, agent_name: str, *, page: int = 0, page_size: int = 20) -> str:
        lines = [
            f"GitHub repos · {agent_name}",
            "GitHub App: active" if self.mode == "app" else "GitHub App: not active",
        ]
        if self.working_repo:
            lines.append(f"Working repo: {self.working_repo}")
        if not self.repos:
            lines.append("No repos connected to this workspace. Connect GitHub first.")
        for repo in self.repos[page * page_size : (page + 1) * page_size]:
            grant = (
                f"{repo.baseline} baseline / {repo.ceiling} ceiling"
                if repo.baseline is not None
                else "no grant"
            )
            if repo.staged:
                live = (
                    f"{repo.live_baseline} baseline / {repo.live_ceiling} ceiling"
                    if repo.live_baseline is not None
                    else "no grant"
                )
                lines.append(
                    f"{repo.full_name} · staged {grant} · live {live} · limit {repo.max_access}"
                )
            else:
                lines.append(f"{repo.full_name} · live {grant} · limit {repo.max_access}")
        reachable = [r.full_name for r in self.repos if r.live_baseline not in (None, "none")]
        shown = ", ".join(reachable[:10])
        more = f" +{len(reachable) - 10} more" if len(reachable) > 10 else ""
        lines.append("Can reach: " + ((shown + more) if reachable else "none"))
        if len(self.repos) > page_size:
            lines.append(f"Page {page + 1} of {(len(self.repos) + page_size - 1) // page_size}")
        return "\n".join(lines)


async def load_grants_panel(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> GrantsPanel:
    authorized = await github_access.list_authorized_repos(session, tenant_id=tenant_id)
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
    mode = await github_access.get_agent_mode(session, tenant_id=tenant_id, agent_id=agent_id)
    binding = await agent_repo_binding.get_binding(session, tenant_id=tenant_id, agent_id=agent_id)
    overlay = await agent_github_binding.get_agent_github_binding(session, agent_id=agent_id)
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
            staged=row.repo_id in drafts
            or (grants[row.repo_id].staged if row.repo_id in grants else False),
            working=(
                drafts[row.repo_id].is_working_repo
                if drafts[row.repo_id].operation == "upsert"
                else False
            )
            if row.repo_id in drafts
            else (grants[row.repo_id].is_working_repo if row.repo_id in grants else False),
            live_baseline=(
                grants[row.repo_id].baseline_access
                if row.repo_id in grants and not grants[row.repo_id].staged and mode == "app"
                else None
            ),
            live_ceiling=(
                grants[row.repo_id].ceiling_access
                if row.repo_id in grants and not grants[row.repo_id].staged and mode == "app"
                else None
            ),
        )
        for row in authorized
        if row.status == "active"
    )
    return GrantsPanel(
        mode=mode,
        repos=repos,
        working_repo=binding.repo_url if binding else None,
        has_pat=overlay is not None,
        has_pending=bool(drafts) or any(row.staged for row in grants.values()),
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
    mode = await github_access.get_agent_mode(session, tenant_id=tenant_id, agent_id=agent_id)
    if mode == "legacy":
        await github_access.stage_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            baseline_access=baseline_access,
            ceiling_access=ceiling_access,
            granted_by_account_id=account_id,
            is_working_repo=is_working_repo,
        )
        return
    authorized = await session.get(TenantGitHubRepo, (tenant_id, repo_id), with_for_update=True)
    if authorized is None or authorized.status != "active":
        raise ValueError("Repository is not connected to this workspace.")
    installation = await session.get(GitHubAppInstallation, authorized.installation_id)
    if installation is None or installation.suspended_at is not None:
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
    mode = await github_access.get_agent_mode(session, tenant_id=tenant_id, agent_id=agent_id)
    if mode == "legacy":
        await github_access.remove_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=repo_id,
            changed_by_account_id=account_id,
        )
        return
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
) -> bool:
    """Switch only after working-repo coverage; retire a per-agent PAT atomically."""
    panel = await load_grants_panel(session, tenant_id=tenant_id, agent_id=agent_id)
    if panel.working_repo is not None:
        working = next(
            (r for r in panel.repos if r.full_name.casefold() == panel.working_repo.casefold()),
            None,
        )
        if working is None:
            raise ValueError("Connect the working repo first.")
        if working.baseline != "write":
            raise ValueError("Stage write access to the working repo first.")
    skill_repos = await session.scalars(
        select(AgentSkillRepoCredential).where(
            AgentSkillRepoCredential.tenant_id == tenant_id,
            AgentSkillRepoCredential.agent_id == agent_id,
        )
    )
    for skill_repo in skill_repos:
        if skill_repo.proof_kind == "public":
            continue
        skill = next(
            (r for r in panel.repos if r.full_name.casefold() == skill_repo.repo_url.casefold()),
            None,
        )
        if skill is None:
            raise ValueError("Connect the agent's skill repo first.")
        if skill.baseline in (None, "none"):
            raise ValueError("Stage read access to the agent's skill repo first.")
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
    for draft in drafts:
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
                baseline_access=draft.baseline_access,
                ceiling_access=draft.ceiling_access,
                granted_by_account_id=account_id,
                is_working_repo=draft.is_working_repo,
            )
    await github_access.activate_agent(
        session, tenant_id=tenant_id, agent_id=agent_id, changed_by_account_id=account_id
    )
    if drafts:
        await session.execute(
            delete(AgentGitHubGrantDraft).where(
                AgentGitHubGrantDraft.tenant_id == tenant_id,
                AgentGitHubGrantDraft.agent_id == agent_id,
            )
        )
    overlay = await agent_github_binding.get_agent_github_binding(session, agent_id=agent_id)
    removed_pat = overlay is not None
    if overlay is not None:
        await agent_github_binding.delete_for_agent(session, agent_id=agent_id)
        if overlay.principal_id == agent_id:
            await delete_credential_for_principal(session, principal_id=agent_id)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        await agent_files.delete_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_id, key=key
        )
    if removed_pat:
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
            reason="per-agent PAT retired",
        )
    return removed_pat
