"""Agent-owned repo rows can be removed and restored on a database with data."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def _repo_insert(scope: str) -> str:
    return (
        "INSERT INTO tenant_github_repos (tenant_id, repo_id, owner_id, installation_id, "
        "repo_full_name, max_access, authorized_by_github_user_id"
        + (", scope_agent_id" if scope else "")
        + ") VALUES (:tenant, 101, 1, 1, 'team/app', 'write', 1"
        + (f", {scope}" if scope else "")
        + ")"
    )


def _grant_insert(agent: str) -> str:
    return (
        "INSERT INTO agent_github_grants (tenant_id, agent_id, repo_id, baseline_access, "
        f"ceiling_access) VALUES (:tenant, {agent}, 101, 'read', 'read')"
    )


@pytest.mark.fresh_schema
async def test_github_agent_repos_migration_round_trips(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0079_github_agent_repos.py"
    spec = importlib.util.spec_from_file_location("migration_github_agent_repos", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def run(step: str) -> Callable[[Connection], None]:
        def inner(sync_conn: Connection) -> None:
            with Operations.context(MigrationContext.configure(sync_conn)):
                getattr(migration, step)()

        return inner

    async def has_column(table: str, column: str) -> bool:
        return bool(
            await db_session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = :table AND column_name = :column)"
                ),
                {"table": table, "column": column},
            )
        )

    tenant, shared_agent, own_agent = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    params = {"tenant": tenant, "shared": shared_agent, "own": own_agent}
    await db_session.execute(
        text("INSERT INTO tenants (id, platform, external_id) VALUES (:tenant, 'discord', 'g')"),
        params,
    )
    await db_session.execute(text(_repo_insert("")), params)
    await db_session.execute(text(_repo_insert(":own")), params)
    await db_session.execute(text(_grant_insert(":shared")), params)
    await db_session.execute(text(_grant_insert(":own")), params)

    await conn.run_sync(run("downgrade"))
    assert not await has_column("tenant_github_repos", "scope_agent_id")
    assert not await has_column("github_connect_invitations", "agent_ma_id")
    # The agent's own row is gone; the server-wide row and every grant on it remain.
    assert await db_session.scalar(text("SELECT count(*) FROM tenant_github_repos")) == 1
    assert await db_session.scalar(text("SELECT count(*) FROM agent_github_grants")) == 2
    # And grants reference repos again.
    await db_session.execute(text("DELETE FROM tenant_github_repos"))
    assert await db_session.scalar(text("SELECT count(*) FROM agent_github_grants")) == 0

    await db_session.execute(text(_repo_insert("")), params)
    await db_session.execute(text(_grant_insert(":shared")), params)
    await conn.run_sync(run("upgrade"))
    assert await has_column("tenant_github_repos", "scope_agent_id")
    assert await has_column("github_connect_click_intents", "agent_ma_id")
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM tenant_github_repos WHERE id IS NOT NULL")
        )
        == 1
    )
    # One row per scope: a second server-wide row clashes, an agent's own row does not.
    await db_session.execute(text(_repo_insert(":own")), params)
    with pytest.raises(IntegrityError, match="uq_tenant_github_repos_scope"):
        async with db_session.begin_nested():
            await db_session.execute(text(_repo_insert("")), params)
    # Grants outlive their repo row now; the stores remove them with it.
    await db_session.execute(text("DELETE FROM tenant_github_repos"))
    assert await db_session.scalar(text("SELECT count(*) FROM agent_github_grants")) == 1
    await db_session.execute(text("DELETE FROM tenants WHERE id = :tenant"), params)
    assert await db_session.scalar(text("SELECT count(*) FROM agent_github_grants")) == 0
