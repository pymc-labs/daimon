"""The data migration marks the working repo of agents already in App mode."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import NamedTuple

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession


class _Case(NamedTuple):
    mode: str
    binding: str | None
    grants: list[tuple[int, bool, bool]]  # repo_id, staged, working
    working: set[int]  # repo ids expected to be the working repo afterwards


@pytest.mark.fresh_schema
async def test_marks_only_an_unambiguous_live_match_in_app_mode(db_session: AsyncSession) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0096_keep_legacy_working_repo.py"
    spec = importlib.util.spec_from_file_location("migration_keep_legacy_working_repo", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    tenant = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO tenants (id, platform, external_id) VALUES (:t, 'discord', 'g')"),
        {"t": tenant},
    )
    repos = {101: "Example/Work", 102: "example/other", 201: "example/dup", 202: "Example/Dup"}
    for repo_id, name in repos.items():
        await db_session.execute(
            text(
                "INSERT INTO tenant_github_repos (tenant_id, repo_id, owner_id, installation_id, "
                "repo_full_name, max_access, authorized_by_github_user_id) "
                "VALUES (:t, :r, 1, 1, :n, 'write', 1)"
            ),
            {"t": tenant, "r": repo_id, "n": name},
        )
    cases: dict[str, _Case] = {
        "plain": _Case("app", "example/work", [(101, False, False), (102, False, False)], {101}),
        "url_form": _Case(
            "app", "https://github.com/EXAMPLE/work.git/", [(101, False, False)], {101}
        ),
        "already_marked": _Case(
            "app", "example/work", [(101, False, False), (102, False, True)], {102}
        ),
        "no_binding": _Case("app", None, [(101, False, False)], set()),
        "not_granted": _Case("app", "example/work", [(102, False, False)], set()),
        "legacy_mode": _Case("legacy", "example/work", [(101, False, False)], set()),
        "staged_only": _Case("app", "example/work", [(101, True, False)], set()),
        "ambiguous": _Case("app", "example/dup", [(201, False, False), (202, False, False)], set()),
    }
    agents = {name: uuid.uuid4() for name in cases}
    for name, case in cases.items():
        mode, binding, grants = case.mode, case.binding, case.grants
        params = {"t": tenant, "a": agents[name]}
        await db_session.execute(
            text("INSERT INTO agent_github_mode (tenant_id, agent_id, mode) VALUES (:t, :a, :m)"),
            {**params, "m": mode},
        )
        if binding is not None:
            await db_session.execute(
                text(
                    "INSERT INTO agent_repo_binding "
                    "(tenant_id, agent_id, repo_url, default_branch, ma_secret_ref) "
                    "VALUES (:t, :a, :u, 'main', 'k')"
                ),
                {**params, "u": binding},
            )
        for repo_id, staged, working in grants:
            await db_session.execute(
                text(
                    "INSERT INTO agent_github_grants (tenant_id, agent_id, repo_id, "
                    "baseline_access, ceiling_access, staged, is_working_repo) "
                    "VALUES (:t, :a, :r, 'write', 'write', :s, :w)"
                ),
                {**params, "r": repo_id, "s": staged, "w": working},
            )

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    conn = await db_session.connection()
    await conn.run_sync(upgrade)

    for name, case in cases.items():
        rows = await db_session.execute(
            text(
                "SELECT repo_id, version FROM agent_github_grants "
                "WHERE agent_id = :a AND is_working_repo"
            ),
            {"a": agents[name]},
        )
        found = {row.repo_id: row.version for row in rows}
        assert set(found) == case.working, name
    # A marked grant moves to version 2 and is audited once; nothing else changes.
    marked = {"plain", "url_form"}
    audited = await db_session.execute(
        text(
            "SELECT agent_id, github_repo_ids FROM security_audit_events "
            "WHERE reason = 'working repo kept from legacy binding'"
        )
    )
    assert {row.agent_id: row.github_repo_ids for row in audited} == {
        agents[name]: [101] for name in marked
    }
    version = await db_session.scalar(
        text("SELECT version FROM agent_github_grants WHERE agent_id = :a AND repo_id = 101"),
        {"a": agents["plain"]},
    )
    assert version == 2
    untouched = await db_session.scalar(
        text("SELECT version FROM agent_github_grants WHERE agent_id = :a AND repo_id = 102"),
        {"a": agents["plain"]},
    )
    assert untouched == 1

    # Running it again changes nothing.
    await conn.run_sync(upgrade)
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM security_audit_events WHERE tool_name = 'github_grants'")
        )
        == 2
    )
