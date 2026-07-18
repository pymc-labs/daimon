"""Real-DB behavior tests for the github_app_installations store."""

from __future__ import annotations

import pytest
from daimon.core.errors import StoreError
from daimon.core.stores import github_app_installations as store
from daimon.core.stores.domain import GitHubAppInstallationRow
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_upsert_creates_new_installation(db_session: AsyncSession) -> None:
    """INS-01: upsert creates a new installation row with all fields populated."""
    row = await store.upsert(
        db_session,
        installation_id=1001,
        account_login="octocat",
        repo_full_names=["octocat/hello-world", "octocat/my-repo"],
    )

    assert isinstance(row, GitHubAppInstallationRow), "upsert must return Pydantic, not ORM"
    assert row.installation_id == 1001, "installation_id should round-trip"
    assert row.account_login == "octocat", "account_login should round-trip"
    assert set(row.repo_full_names) == {"octocat/hello-world", "octocat/my-repo"}, (
        "repo_full_names should round-trip"
    )
    assert row.created_at is not None, "created_at should be set by server_default"
    assert row.updated_at is not None, "updated_at should be set by server_default"


@pytest.mark.asyncio
async def test_upsert_updates_existing_installation(db_session: AsyncSession) -> None:
    """INS-02: upsert on conflict overwrites the row atomically (full-set rewrite)."""
    await store.upsert(
        db_session,
        installation_id=1002,
        account_login="original",
        repo_full_names=["org/old-repo"],
    )

    updated = await store.upsert(
        db_session,
        installation_id=1002,
        account_login="updated-login",
        repo_full_names=["org/new-repo"],
    )

    assert updated.account_login == "updated-login", "upsert must overwrite account_login"
    assert list(updated.repo_full_names) == ["org/new-repo"], (
        "upsert must replace repo_full_names (full-set rewrite)"
    )
    assert "org/old-repo" not in updated.repo_full_names, (
        "old repo should be gone after full-set rewrite"
    )


@pytest.mark.asyncio
async def test_upsert_is_idempotent(db_session: AsyncSession) -> None:
    """INS-03: calling upsert twice with identical data produces one row."""
    await store.upsert(
        db_session,
        installation_id=1003,
        account_login="myorg",
        repo_full_names=["myorg/repo"],
    )
    row2 = await store.upsert(
        db_session,
        installation_id=1003,
        account_login="myorg",
        repo_full_names=["myorg/repo"],
    )

    assert row2.installation_id == 1003, "idempotent upsert should not change installation_id"
    assert list(row2.repo_full_names) == ["myorg/repo"], (
        "idempotent upsert should not duplicate repos"
    )


@pytest.mark.asyncio
async def test_add_repos_unions_repo_set(db_session: AsyncSession) -> None:
    """INS-04: add_repos appends new repos without duplicating existing ones."""
    await store.upsert(
        db_session,
        installation_id=2001,
        account_login="org",
        repo_full_names=["org/alpha"],
    )

    row = await store.add_repos(
        db_session,
        installation_id=2001,
        repos=["org/beta", "org/alpha"],  # alpha already exists
    )

    assert "org/alpha" in row.repo_full_names, "original repo should still be present"
    assert "org/beta" in row.repo_full_names, "newly added repo should appear"
    assert row.repo_full_names.count("org/alpha") == 1, "no duplicate after union"


@pytest.mark.asyncio
async def test_add_repos_raises_when_no_installation(db_session: AsyncSession) -> None:
    """INS-05: add_repos raises StoreError when no installation row exists."""
    with pytest.raises(StoreError, match="no installation for id 9999"):
        await store.add_repos(db_session, installation_id=9999, repos=["org/repo"])


@pytest.mark.asyncio
async def test_remove_repos_drops_specified_repos(db_session: AsyncSession) -> None:
    """INS-06: remove_repos removes the listed repos, leaving others untouched."""
    await store.upsert(
        db_session,
        installation_id=3001,
        account_login="org",
        repo_full_names=["org/a", "org/b", "org/c"],
    )

    row = await store.remove_repos(
        db_session,
        installation_id=3001,
        repos=["org/b"],
    )

    assert "org/a" in row.repo_full_names, "untouched repo org/a should remain"
    assert "org/c" in row.repo_full_names, "untouched repo org/c should remain"
    assert "org/b" not in row.repo_full_names, "removed repo org/b should be gone"


@pytest.mark.asyncio
async def test_remove_repos_raises_when_no_installation(db_session: AsyncSession) -> None:
    """INS-07: remove_repos raises StoreError when no installation row exists."""
    with pytest.raises(StoreError, match="no installation for id 8888"):
        await store.remove_repos(db_session, installation_id=8888, repos=["org/repo"])


@pytest.mark.asyncio
async def test_delete_installation_removes_row(db_session: AsyncSession) -> None:
    """INS-08: delete_installation removes the row; subsequent get returns None."""
    await store.upsert(
        db_session,
        installation_id=4001,
        account_login="org",
        repo_full_names=["org/repo"],
    )

    await store.delete_installation(db_session, installation_id=4001)

    row = await store.get(db_session, installation_id=4001)
    assert row is None, "installation should be gone after delete"


@pytest.mark.asyncio
async def test_delete_installation_raises_when_no_row(db_session: AsyncSession) -> None:
    """INS-09: delete_installation raises StoreError when no row exists."""
    with pytest.raises(StoreError, match="no installation for id 7777"):
        await store.delete_installation(db_session, installation_id=7777)


@pytest.mark.asyncio
async def test_get_returns_row_when_exists(db_session: AsyncSession) -> None:
    """INS-10: get returns GitHubAppInstallationRow when the row exists."""
    await store.upsert(
        db_session,
        installation_id=5001,
        account_login="myuser",
        repo_full_names=["myuser/project"],
    )

    row = await store.get(db_session, installation_id=5001)

    assert row is not None, "get should find the installation row"
    assert isinstance(row, GitHubAppInstallationRow), "get must return Pydantic, not ORM"
    assert row.installation_id == 5001, "installation_id should match"
    assert row.account_login == "myuser", "account_login should match"


@pytest.mark.asyncio
async def test_get_returns_none_when_missing(db_session: AsyncSession) -> None:
    """INS-11: get returns None when no row exists for the installation_id."""
    row = await store.get(db_session, installation_id=6666)

    assert row is None, "missing installation should return None, not raise"


@pytest.mark.asyncio
async def test_get_for_repo_matches_containing_installation(db_session: AsyncSession) -> None:
    """INS-12: get_for_repo returns the installation whose repo_full_names contains the repo."""
    await store.upsert(
        db_session,
        installation_id=6001,
        account_login="org",
        repo_full_names=["org/starter-kit", "org/other-repo"],
    )

    row = await store.get_for_repo(db_session, repo_full_name="org/starter-kit")

    assert row is not None, "get_for_repo should find the matching installation"
    assert row.installation_id == 6001, "should return the installation that contains the repo"
    assert "org/starter-kit" in row.repo_full_names, "repo should appear in the returned row"


@pytest.mark.asyncio
async def test_get_for_repo_returns_none_on_miss(db_session: AsyncSession) -> None:
    """INS-13: get_for_repo returns None when no installation contains the repo."""
    await store.upsert(
        db_session,
        installation_id=6002,
        account_login="org",
        repo_full_names=["org/other-repo"],
    )

    row = await store.get_for_repo(db_session, repo_full_name="org/not-installed")

    assert row is None, "get_for_repo should return None for a repo not in any installation"
