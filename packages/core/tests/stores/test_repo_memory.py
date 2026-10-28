"""Repository pool isolation and guarded replacement against real Postgres."""

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.stores.repo_memory import (
    MemoryEntry,
    delete_entry,
    get_entry,
    list_entries,
    save_entry,
)
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_shared_and_personal_slots(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    bob = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    shared = MemoryEntry(
        tenant_id=tenant.id,
        repo_id=17,
        name="rules",
        title="Rules",
        body="Use uv",
        kind="directive",
        scope="repo",
        created_by_account_id=alice.id,
        created_at=now,
        updated_at=now,
    )
    assert await save_entry(db_session, entry=shared) is None
    assert (
        await save_entry(
            db_session,
            entry=shared.model_copy(update={"created_by_account_id": bob.id, "body": "Use ruff"}),
        )
        == "Rules"
    )
    personal = shared.model_copy(update={"name": "mine", "scope": "user"})
    await save_entry(db_session, entry=personal)
    with pytest.raises(ValueError, match="unavailable"):
        await save_entry(
            db_session, entry=personal.model_copy(update={"created_by_account_id": bob.id})
        )
    with pytest.raises(ValueError, match="scope"):
        await save_entry(db_session, entry=personal.model_copy(update={"scope": "repo"}))
    assert [
        m.name
        for m in await list_entries(db_session, tenant_id=tenant.id, repo_id=17, account_id=bob.id)
    ] == ["rules"]
    assert not await delete_entry(
        db_session, tenant_id=tenant.id, repo_id=17, name="mine", account_id=bob.id
    )
    assert await delete_entry(
        db_session, tenant_id=tenant.id, repo_id=17, name="rules", account_id=alice.id
    )
    assert (await get_entry(db_session, tenant_id=tenant.id, repo_id=17, name="mine")) == personal
    assert not await list_entries(db_session, tenant_id=tenant.id, repo_id=18, account_id=alice.id)
    assert not await list_entries(
        db_session, tenant_id=uuid.uuid4(), repo_id=17, account_id=alice.id
    )


async def test_import_replay_conflict_and_erasure(db_session: AsyncSession) -> None:
    from daimon.core.stores.accounts import delete_account
    from daimon.core.stores.repo_memory import import_entry

    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    entry = MemoryEntry(
        tenant_id=tenant.id,
        repo_id=17,
        name="shared",
        title="Shared",
        body="Fact",
        kind="reference",
        scope="repo",
        created_by_account_id=owner.id,
        source_id="source",
        created_at=now,
        updated_at=now,
    )
    assert await import_entry(db_session, entry=entry)
    assert not await import_entry(db_session, entry=entry)
    with pytest.raises(ValueError, match="conflicts"):
        await import_entry(db_session, entry=entry.model_copy(update={"body": "Different"}))
    personal = entry.model_copy(update={"name": "personal", "kind": "directive", "scope": "user"})
    await save_entry(db_session, entry=personal)
    await delete_account(db_session, account_id=owner.id)
    assert await get_entry(db_session, tenant_id=tenant.id, repo_id=17, name="personal") is None
    remaining = await get_entry(db_session, tenant_id=tenant.id, repo_id=17, name="shared")
    assert remaining is not None and remaining.body == "Fact"
    assert remaining.created_by_account_id is None


async def test_turn_query_is_single_bounded_read(db_session: AsyncSession) -> None:
    from daimon.core._models import AgentGitHubGrant, GitHubAppInstallation, TenantGitHubRepo
    from daimon.core.stores.repo_memory import turn_entries
    from sqlalchemy import event, update

    tenant = await make_tenant(db_session)
    alice = await make_account(db_session, tenant=tenant)
    bob = await make_account(db_session, tenant=tenant)
    agent = uuid.uuid4()
    db_session.add(
        GitHubAppInstallation(
            installation_id=901,
            account_login="owner",
            repo_full_names=["owner/repo"],
            app="github_app",
        )
    )
    db_session.add(
        TenantGitHubRepo(
            tenant_id=tenant.id,
            repo_id=17,
            owner_id=9,
            installation_id=901,
            repo_full_name="owner/repo",
            max_access="read",
            authorized_by_github_user_id=9,
            authorized_by_account_id=alice.id,
            status="active",
        )
    )
    db_session.add(
        AgentGitHubGrant(
            tenant_id=tenant.id,
            agent_id=agent,
            repo_id=17,
            baseline_access="read",
            ceiling_access="read",
            is_working_repo=True,
        )
    )
    await db_session.flush()
    now = datetime.now(UTC)
    shared = MemoryEntry(
        tenant_id=tenant.id,
        repo_id=17,
        name="fact",
        title="Fact",
        body="Do not preload me",
        kind="reference",
        scope="repo",
        created_by_account_id=alice.id,
        created_at=now,
        updated_at=now,
    )
    await save_entry(db_session, entry=shared)
    await save_entry(
        db_session,
        entry=shared.model_copy(
            update={"name": "alice", "scope": "user", "kind": "directive", "body": "Alice only"}
        ),
    )
    await save_entry(
        db_session,
        entry=shared.model_copy(
            update={
                "name": "bob",
                "scope": "user",
                "kind": "directive",
                "body": "Bob only",
                "created_by_account_id": bob.id,
            }
        ),
    )
    connection = await db_session.connection()
    queries: list[str] = []

    def capture(*args: object) -> None:
        queries.append(str(args[2]))

    event.listen(connection.sync_connection, "before_cursor_execute", capture)
    try:
        repo, rows = await turn_entries(
            db_session, tenant_id=tenant.id, agent_id=agent, account_id=alice.id
        )
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", capture)
    assert len(queries) == 1, "grant resolution and catalog injection share one SQL round trip"
    assert repo == "owner/repo"
    assert {row[0] for row in rows} == {"alice", "fact"}
    assert next(row for row in rows if row[0] == "fact")[-1] == ""
    assert "Bob only" not in str(rows)
    _, routine = await turn_entries(
        db_session, tenant_id=tenant.id, agent_id=agent, account_id=None
    )
    assert [row[0] for row in routine] == ["fact"]
    await db_session.execute(update(GitHubAppInstallation).values(suspended_at=now))
    assert await turn_entries(
        db_session, tenant_id=tenant.id, agent_id=agent, account_id=alice.id
    ) == ("", [])
