"""Install-tracking for App-token minting.

Maps installation_id -> (account_login, repo_full_names) so the webhook
handler can mint installation tokens without a per-request GitHub API call.

No try/except — exceptions propagate (per architecture rule).
No module-level singletons.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from daimon.core._models import GitHubAppInstallation, TenantGitHubRepo
from daimon.core.stores.domain import GitHubAppInstallationRow
from sqlalchemy import any_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert(
    session: AsyncSession,
    *,
    installation_id: int,
    account_login: str,
    repo_full_names: list[str],
) -> GitHubAppInstallationRow:
    """Replace the cached repository set with a complete trusted snapshot."""
    stmt = (
        pg_insert(GitHubAppInstallation)
        .values(
            installation_id=installation_id,
            account_login=account_login,
            repo_full_names=repo_full_names,
        )
        .on_conflict_do_update(
            index_elements=["installation_id"],
            set_={
                "account_login": account_login,
                "repo_full_names": repo_full_names,
                "updated_at": func.now(),
            },
            where=GitHubAppInstallation.app == "legacy",
        )
        .returning(GitHubAppInstallation)
    )
    result = await session.execute(stmt.execution_options(populate_existing=True))
    orm = result.scalar_one()
    await session.flush()
    return GitHubAppInstallationRow.model_validate(orm)


async def upsert_github_app(
    session: AsyncSession,
    *,
    installation_id: int,
    account_id: int,
    account_login: str,
    account_type: str,
    repository_selection: Literal["all", "selected"],
    suspended_at: datetime | None,
) -> GitHubAppInstallationRow:
    """Record new-App metadata and the confirmed repository set in one transaction."""
    await session.flush()
    names = await session.scalars(
        select(TenantGitHubRepo.repo_full_name).where(
            TenantGitHubRepo.installation_id == installation_id,
            TenantGitHubRepo.status == "active",
        )
    )
    repo_full_names = sorted(set(names))
    values = {
        "account_id": account_id,
        "account_login": account_login,
        "account_type": account_type,
        "repository_selection": repository_selection,
        "suspended_at": suspended_at,
        "repo_full_names": repo_full_names,
        "app": "github_app",
    }
    stmt = (
        pg_insert(GitHubAppInstallation)
        .values(installation_id=installation_id, **values)
        .on_conflict_do_update(
            index_elements=["installation_id"],
            set_={**values, "updated_at": func.now()},
        )
        .returning(GitHubAppInstallation)
    )
    orm = (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one()
    await session.flush()
    return GitHubAppInstallationRow.model_validate(orm)


async def get(
    session: AsyncSession,
    *,
    installation_id: int,
) -> GitHubAppInstallationRow | None:
    """Point read by installation_id. Returns None when not found."""
    orm = await session.get(GitHubAppInstallation, installation_id)
    if orm is None:
        return None
    return GitHubAppInstallationRow.model_validate(orm)


async def get_for_repo(
    session: AsyncSession,
    *,
    repo_full_name: str,
) -> GitHubAppInstallationRow | None:
    """Find the installation whose repo_full_names contains the given repo.

    Used to determine whether an App installation token can be minted for
    a given repo (credential priority: App token -> PAT -> anon).
    Returns the first matching row or None.
    """
    stmt = select(GitHubAppInstallation).where(
        GitHubAppInstallation.app == "legacy",
        any_(GitHubAppInstallation.repo_full_names) == repo_full_name,
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        return None
    return GitHubAppInstallationRow.model_validate(orm)
