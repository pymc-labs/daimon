"""Install-tracking for App-token minting.

Maps installation_id -> (account_login, repo_full_names) so the webhook
handler can mint installation tokens without a per-request GitHub API call.

No try/except — exceptions propagate (per architecture rule).
No module-level singletons.
"""

from __future__ import annotations

from typing import Any, cast

from daimon.core._models import GitHubAppInstallation
from daimon.core.errors import StoreError
from daimon.core.stores.domain import GitHubAppInstallationRow
from sqlalchemy import CursorResult, any_, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def upsert(
    session: AsyncSession,
    *,
    installation_id: int,
    account_login: str,
    repo_full_names: list[str],
) -> GitHubAppInstallationRow:
    """Upsert the installation record (full-set write).

    Used by `installation` (created) and `installation_repositories`
    (full-set rewrite) events. Overwrites the repo list atomically.
    """
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
        )
        .returning(GitHubAppInstallation)
    )
    result = await session.execute(stmt.execution_options(populate_existing=True))
    orm = result.scalar_one()
    await session.flush()
    return GitHubAppInstallationRow.model_validate(orm)


async def _load(
    session: AsyncSession,
    installation_id: int,
) -> GitHubAppInstallation:
    """Load the current row from DB, bypassing the identity-map cache.

    Raises StoreError when not found.
    """
    result = await session.execute(
        select(GitHubAppInstallation).where(
            GitHubAppInstallation.installation_id == installation_id
        )
    )
    orm = result.scalar_one_or_none()
    if orm is None:
        raise StoreError(f"no installation for id {installation_id}")
    return orm


async def add_repos(
    session: AsyncSession,
    *,
    installation_id: int,
    repos: list[str],
) -> GitHubAppInstallationRow:
    """Union-add repos to an existing installation (repositories_added event).

    Raises StoreError when no installation row exists (no row to extend).
    """
    orm = await _load(session, installation_id)
    existing = list(orm.repo_full_names)
    merged = list(dict.fromkeys(existing + repos))  # dedup, preserve order
    return await upsert(
        session,
        installation_id=installation_id,
        account_login=orm.account_login,
        repo_full_names=merged,
    )


async def remove_repos(
    session: AsyncSession,
    *,
    installation_id: int,
    repos: list[str],
) -> GitHubAppInstallationRow:
    """Drop repos from an existing installation (repositories_removed event).

    Raises StoreError when no installation row exists.
    """
    orm = await _load(session, installation_id)
    to_remove = set(repos)
    remaining = [r for r in orm.repo_full_names if r not in to_remove]
    return await upsert(
        session,
        installation_id=installation_id,
        account_login=orm.account_login,
        repo_full_names=remaining,
    )


async def delete_installation(
    session: AsyncSession,
    *,
    installation_id: int,
) -> None:
    """Remove an installation record (installation deleted event).

    Raises StoreError when no row exists (mirrors clear_binding discipline).
    """
    result = await session.execute(
        delete(GitHubAppInstallation).where(
            GitHubAppInstallation.installation_id == installation_id
        )
    )
    if cast(CursorResult[Any], result).rowcount == 0:
        raise StoreError(f"no installation for id {installation_id}")
    await session.flush()


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
        any_(GitHubAppInstallation.repo_full_names) == repo_full_name
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        return None
    return GitHubAppInstallationRow.model_validate(orm)
