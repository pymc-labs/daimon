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
        repo, rows, previous = await turn_entries(
            db_session, tenant_id=tenant.id, agent_id=agent, account_id=alice.id
        )
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", capture)
    assert len(queries) == 1, "grant resolution and catalog injection share one SQL round trip"
    assert previous is None
    assert repo == "owner/repo"
    assert {row[0] for row in rows} == {"alice", "fact"}
    assert next(row for row in rows if row[0] == "fact")[-1] == ""
    assert "Bob only" not in str(rows)
    _, routine, _ = await turn_entries(
        db_session, tenant_id=tenant.id, agent_id=agent, account_id=None
    )
    assert [row[0] for row in routine] == ["fact"]
    await db_session.execute(update(GitHubAppInstallation).values(suspended_at=now))
    assert await turn_entries(
        db_session, tenant_id=tenant.id, agent_id=agent, account_id=alice.id
    ) == ("", [], None)


async def test_verified_origin_keeps_memory_restriction(db_session: AsyncSession) -> None:
    from datetime import timedelta

    from daimon.core.stores.domain import Role
    from daimon.core.stores.turn_origins import (
        bind_origin_session,
        create_origin,
        get_active_origin,
    )

    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    origin = await create_origin(
        db_session,
        tenant_id=tenant.id,
        account_id=owner.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="thread",
        responder_ma_agent_id="agent",
        responder_name="Agent",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        now=now,
        expires_at=now + timedelta(minutes=10),
    )
    await bind_origin_session(
        db_session, origin_id=origin.id, chat_session_id="signed", memory_read_only=True
    )
    stamped = await get_active_origin(
        db_session,
        origin_id=origin.id,
        tenant_id=tenant.id,
        account_id=owner.id,
        platform="discord",
        now=now,
    )
    assert stamped is not None and stamped.memory_read_only is True
    # Native backends rebind without an admission stamp; the restriction survives.
    await bind_origin_session(db_session, origin_id=origin.id, chat_session_id=str(origin.id))
    rebound = await get_active_origin(
        db_session,
        origin_id=origin.id,
        tenant_id=tenant.id,
        account_id=owner.id,
        platform="discord",
        now=now,
    )
    assert rebound is not None and rebound.memory_read_only is True
    await bind_origin_session(db_session, origin_id=origin.id, chat_session_id=None)
    cleared = await get_active_origin(
        db_session,
        origin_id=origin.id,
        tenant_id=tenant.id,
        account_id=owner.id,
        platform="discord",
        now=now,
    )
    assert cleared is not None and cleared.memory_read_only is None


async def test_concurrent_personal_first_saves_have_one_owner(
    db_committing_engine, db_clean: None
) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessions = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with sessions.begin() as session:
        tenant = await make_tenant(session)
        alice = await make_account(session, tenant=tenant)
        bob = await make_account(session, tenant=tenant)
    now = datetime.now(UTC)

    async def write(account_id: uuid.UUID) -> str | None:
        async with sessions.begin() as session:
            return await save_entry(
                session,
                entry=MemoryEntry(
                    tenant_id=tenant.id,
                    repo_id=17,
                    name="preference",
                    title="Preference",
                    body=str(account_id),
                    kind="directive",
                    scope="user",
                    created_by_account_id=account_id,
                    created_at=now,
                    updated_at=now,
                ),
            )

    outcomes = await asyncio.gather(write(alice.id), write(bob.id), return_exceptions=True)
    assert sum(result is None for result in outcomes) == 1
    assert sum(isinstance(result, ValueError) for result in outcomes) == 1
    async with sessions() as session:
        entry = await get_entry(session, tenant_id=tenant.id, repo_id=17, name="preference")
    assert entry is not None
    assert entry.body == str(entry.created_by_account_id)


async def test_render_hash_is_scoped_to_tenant_provider_and_session(
    db_session: AsyncSession,
) -> None:
    from daimon.core.stores.repo_memory import record_turn_state, turn_entries

    tenant = await make_tenant(db_session)
    other_tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    # Native sessions have no ThreadSession mapping; they still retain state.
    key = dict(tenant_id=tenant.id, session_id="native-session", session_provider="openai")
    await record_turn_state(db_session, **key, sha256="delivered-state")
    # Even an absent working repo carries the previous hash in the same state query.
    assert await turn_entries(db_session, **key, agent_id=agent_id, account_id=None) == (
        "",
        [],
        "delivered-state",
    )
    for different in (
        {"session_id": "replacement"},
        {"tenant_id": other_tenant.id},
        {"session_provider": "anthropic"},
    ):
        assert (
            await turn_entries(
                db_session,
                **{**key, **different},
                agent_id=agent_id,
                account_id=None,
            )
        )[-1] is None
        await record_turn_state(db_session, **{**key, **different}, sha256="other-session")
    assert (await turn_entries(db_session, **key, agent_id=agent_id, account_id=None))[
        -1
    ] == "delivered-state"
    await record_turn_state(db_session, **key, sha256="updated-state")
    assert (await turn_entries(db_session, **key, agent_id=agent_id, account_id=None))[
        -1
    ] == "updated-state"
