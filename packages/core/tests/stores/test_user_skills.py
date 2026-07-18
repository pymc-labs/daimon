"""Integration tests for user_skills store — real Postgres + UPSERT semantics."""

from __future__ import annotations

import uuid

from daimon.core.stores.user_skills import (
    count_user_skills_for_principal,
    delete_user_skill,
    delete_user_skills_for_principal,
    delete_user_skills_for_repo,
    get_first_user_skill_for_principal,
    list_user_skill_repos_for_agent,
    list_user_skills_for_agent,
    list_user_skills_for_repo,
    load_user_skill,
    upsert_user_skill,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_upsert_user_skill_inserts_new_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    principal_id = uuid.uuid4()

    inserted = await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="dev",
        name="brainstorming",
        source_repo_url="https://github.com/owner/repo",
        source_repo_branch="main",
        source_path="skills/brainstorming",
        content_hash="hash1",
        anthropic_id="skill_abc",
        anthropic_latest_version="v1",
    )

    assert inserted.tenant_id == tenant.id, "round-trip tenant_id"
    assert inserted.principal_id == principal_id, "round-trip principal_id"
    assert inserted.agent_name == "dev"
    assert inserted.name == "brainstorming"
    assert inserted.source_repo_url == "https://github.com/owner/repo"
    assert inserted.source_repo_branch == "main"
    assert inserted.source_path == "skills/brainstorming"
    assert inserted.content_hash == "hash1"
    assert inserted.anthropic_id == "skill_abc"
    assert inserted.anthropic_latest_version == "v1"

    loaded = await load_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="dev",
        name="brainstorming",
    )
    assert loaded is not None, "load must return the row after upsert"
    assert loaded.content_hash == "hash1"


async def test_upsert_user_skill_replaces_on_conflict(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    principal_id = uuid.uuid4()

    first = await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="dev",
        name="brainstorming",
        source_repo_url="https://github.com/owner/repo",
        source_repo_branch="main",
        source_path="skills/brainstorming",
        content_hash="hash1",
        anthropic_id=None,
        anthropic_latest_version=None,
    )

    second = await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="dev",
        name="brainstorming",
        source_repo_url="https://github.com/owner/repo",
        source_repo_branch="main",
        source_path="skills/brainstorming",
        content_hash="hash2",
        anthropic_id="skill_abc",
        anthropic_latest_version="v2",
    )

    assert second.content_hash == "hash2", "UPSERT must replace content_hash on conflict"
    assert second.anthropic_id == "skill_abc", "UPSERT must replace anthropic_id on conflict"
    assert second.anthropic_latest_version == "v2", (
        "UPSERT must replace anthropic_latest_version on conflict"
    )
    assert second.updated_at >= first.updated_at, (
        "UPSERT must bump updated_at (>= since timestamps may tie)"
    )


async def test_load_user_skill_returns_none_when_absent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    row = await load_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=uuid.uuid4(),
        agent_name="dev",
        name="missing",
    )
    assert row is None, "must return None for unknown skill (distinct from 'something broke')"


async def test_list_user_skills_for_agent_returns_only_matching_agent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal_id = uuid.uuid4()

    for skill_name in ("c-skill", "a-skill", "b-skill"):
        await upsert_user_skill(
            db_session,
            tenant_id=tenant.id,
            principal_id=principal_id,
            agent_name="agent_a",
            name=skill_name,
            source_repo_url="https://github.com/owner/repo",
            source_repo_branch="main",
            source_path="",
            content_hash=f"hash-{skill_name}",
            anthropic_id=None,
            anthropic_latest_version=None,
        )

    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="agent_b",
        name="other",
        source_repo_url="https://github.com/owner/repo",
        source_repo_branch="main",
        source_path="",
        content_hash="hash-other",
        anthropic_id=None,
        anthropic_latest_version=None,
    )

    rows = await list_user_skills_for_agent(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="agent_a",
    )
    assert [r.name for r in rows] == ["a-skill", "b-skill", "c-skill"], (
        "list must filter by agent_name and return rows ordered by name ASC"
    )


async def test_list_user_skills_for_agent_returns_empty_when_none(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    rows = await list_user_skills_for_agent(
        db_session,
        tenant_id=tenant.id,
        principal_id=uuid.uuid4(),
        agent_name="dev",
    )
    assert rows == [], "list must return empty list when no skills match"


async def test_delete_user_skill_removes_row(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    principal_id = uuid.uuid4()
    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="dev",
        name="brainstorming",
        source_repo_url="https://github.com/owner/repo",
        source_repo_branch="main",
        source_path="",
        content_hash="hash1",
        anthropic_id=None,
        anthropic_latest_version=None,
    )

    await delete_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="dev",
        name="brainstorming",
    )

    loaded = await load_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_id,
        agent_name="dev",
        name="brainstorming",
    )
    assert loaded is None, "delete must remove the row; subsequent load returns None"


async def test_delete_user_skill_is_no_op_when_absent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    # Must not raise.
    await delete_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=uuid.uuid4(),
        agent_name="dev",
        name="never-existed",
    )


async def test_two_principals_can_hold_same_agent_name_skill_name(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()

    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_a,
        agent_name="x",
        name="brainstorming",
        source_repo_url="https://github.com/a/repo",
        source_repo_branch="main",
        source_path="",
        content_hash="hash-a",
        anthropic_id=None,
        anthropic_latest_version=None,
    )
    await upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_b,
        agent_name="x",
        name="brainstorming",
        source_repo_url="https://github.com/b/repo",
        source_repo_branch="main",
        source_path="",
        content_hash="hash-b",
        anthropic_id=None,
        anthropic_latest_version=None,
    )

    loaded_a = await load_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_a,
        agent_name="x",
        name="brainstorming",
    )
    loaded_b = await load_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_b,
        agent_name="x",
        name="brainstorming",
    )
    assert loaded_a is not None and loaded_b is not None, (
        "both principals should round-trip independently"
    )
    assert loaded_a.content_hash == "hash-a", "principal_a's row preserved"
    assert loaded_b.content_hash == "hash-b", "principal_b's row preserved"
    assert loaded_a.source_repo_url == "https://github.com/a/repo"
    assert loaded_b.source_repo_url == "https://github.com/b/repo"


async def _seed_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    agent_name: str = "dev",
    name: str = "brainstorming",
) -> None:
    await upsert_user_skill(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        agent_name=agent_name,
        name=name,
        source_repo_url="https://github.com/owner/repo",
        source_repo_branch="main",
        source_path="",
        content_hash=f"hash-{name}",
        anthropic_id=None,
        anthropic_latest_version=None,
    )


async def test_delete_user_skills_for_principal_removes_rows_and_returns_rowcount(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal_a = uuid.uuid4()
    principal_b = uuid.uuid4()

    await _seed_skill(db_session, tenant_id=tenant.id, principal_id=principal_a, name="skill-1")
    await _seed_skill(db_session, tenant_id=tenant.id, principal_id=principal_a, name="skill-2")
    await _seed_skill(db_session, tenant_id=tenant.id, principal_id=principal_b, name="skill-1")

    rowcount = await delete_user_skills_for_principal(db_session, principal_id=principal_a)

    assert rowcount == 2, "delete must return the count of rows removed (principal_a had 2)"

    remaining = await list_user_skills_for_agent(
        db_session,
        tenant_id=tenant.id,
        principal_id=principal_b,
        agent_name="dev",
    )
    assert len(remaining) == 1, "principal_b's rows must survive the delete"
    assert remaining[0].name == "skill-1", "principal_b's skill-1 row must be untouched"


async def test_delete_user_skills_for_principal_is_tenant_agnostic(
    db_session: AsyncSession,
) -> None:
    """Rows under different tenant_ids for the same principal must both be deleted."""
    tenant_a = await make_tenant(db_session, workspace_id="guild-a")
    tenant_b = await make_tenant(db_session, workspace_id="guild-b")
    principal_id = uuid.uuid4()

    await _seed_skill(db_session, tenant_id=tenant_a.id, principal_id=principal_id, name="skill-a")
    await _seed_skill(db_session, tenant_id=tenant_b.id, principal_id=principal_id, name="skill-b")

    rowcount = await delete_user_skills_for_principal(db_session, principal_id=principal_id)

    assert rowcount == 2, (
        "delete must remove rows across ALL tenant_ids — ghost rows stranded under stale "
        "tenant_ids by the 71-12 re-key must be erased regardless of tenant bookkeeping"
    )


async def test_delete_user_skills_for_principal_returns_zero_when_no_rows(
    db_session: AsyncSession,
) -> None:
    rowcount = await delete_user_skills_for_principal(db_session, principal_id=uuid.uuid4())
    assert rowcount == 0, "re-run on empty must return 0 (idempotent)"


async def test_count_user_skills_for_principal_matches_delete_rowcount(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal_id = uuid.uuid4()

    await _seed_skill(db_session, tenant_id=tenant.id, principal_id=principal_id, name="skill-1")
    await _seed_skill(db_session, tenant_id=tenant.id, principal_id=principal_id, name="skill-2")
    await _seed_skill(db_session, tenant_id=tenant.id, principal_id=principal_id, name="skill-3")

    count_before = await count_user_skills_for_principal(db_session, principal_id=principal_id)
    deleted = await delete_user_skills_for_principal(db_session, principal_id=principal_id)

    assert count_before == deleted, (
        "count helper must equal the rowcount delete returns for the same seed (parity)"
    )
    assert count_before == 3, "expected 3 seeded rows"


async def test_get_first_user_skill_for_principal_returns_name_ordered_first(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    principal_id = uuid.uuid4()

    for name in ("zebra", "alpha", "mango"):
        await _seed_skill(db_session, tenant_id=tenant.id, principal_id=principal_id, name=name)

    first = await get_first_user_skill_for_principal(db_session, principal_id=principal_id)

    assert first is not None, "get_first must return a row when skills exist"
    assert first.name == "alpha", "get_first must return the name-ordered first row"


async def test_get_first_user_skill_for_principal_returns_none_when_no_rows(
    db_session: AsyncSession,
) -> None:
    first = await get_first_user_skill_for_principal(db_session, principal_id=uuid.uuid4())
    assert first is None, "get_first must return None when no rows exist for the principal"


async def _seed_repo_skill(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    principal_id: uuid.UUID,
    agent_name: str,
    name: str,
    repo_url: str,
) -> None:
    await upsert_user_skill(
        session,
        tenant_id=tenant_id,
        principal_id=principal_id,
        agent_name=agent_name,
        name=name,
        source_repo_url=repo_url,
        source_repo_branch="main",
        source_path="",
        content_hash=f"hash-{name}",
        anthropic_id=f"skill_{name}",
        anthropic_latest_version="1",
    )


async def test_list_user_skill_repos_for_agent_returns_distinct_sorted_urls(
    db_session: AsyncSession,
) -> None:
    """Distinct repos for an agent, across principals — the de-facto repo list."""
    tenant = await make_tenant(db_session)
    repo_a = "https://github.com/a/one"
    repo_b = "https://github.com/b/two"
    # Two skills from repo_b under one principal, one from repo_a under another.
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=uuid.uuid4(),
        agent_name="agent",
        name="x",
        repo_url=repo_b,
    )
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=uuid.uuid4(),
        agent_name="agent",
        name="y",
        repo_url=repo_b,
    )
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=uuid.uuid4(),
        agent_name="agent",
        name="z",
        repo_url=repo_a,
    )
    # A different agent's repo must not leak in.
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=uuid.uuid4(),
        agent_name="other",
        name="w",
        repo_url="https://github.com/c/three",
    )

    repos = await list_user_skill_repos_for_agent(
        db_session, tenant_id=tenant.id, agent_name="agent"
    )
    assert repos == [repo_a, repo_b], (
        "must return the agent's distinct repos, sorted, with no other agent's repo"
    )


async def test_list_user_skills_for_repo_is_principal_agnostic(
    db_session: AsyncSession,
) -> None:
    """All rows for one (agent, repo) regardless of principal_id."""
    tenant = await make_tenant(db_session)
    repo = "https://github.com/a/one"
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=p1,
        agent_name="agent",
        name="a",
        repo_url=repo,
    )
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=p2,
        agent_name="agent",
        name="b",
        repo_url=repo,
    )
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=p1,
        agent_name="agent",
        name="c",
        repo_url="https://github.com/other/repo",
    )

    rows = await list_user_skills_for_repo(
        db_session, tenant_id=tenant.id, agent_name="agent", source_repo_url=repo
    )
    assert {r.name for r in rows} == {"a", "b"}, (
        "must return both repo rows across principals, excluding the other repo's row"
    )


async def test_delete_user_skills_for_repo_removes_only_that_repo_and_returns_count(
    db_session: AsyncSession,
) -> None:
    """Deletes every (agent, repo) row across principals; leaves other repos intact."""
    tenant = await make_tenant(db_session)
    repo = "https://github.com/a/one"
    keep = "https://github.com/keep/repo"
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=p1,
        agent_name="agent",
        name="a",
        repo_url=repo,
    )
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=p2,
        agent_name="agent",
        name="b",
        repo_url=repo,
    )
    await _seed_repo_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=p1,
        agent_name="agent",
        name="keeper",
        repo_url=keep,
    )

    removed = await delete_user_skills_for_repo(
        db_session, tenant_id=tenant.id, agent_name="agent", source_repo_url=repo
    )
    assert removed == 2, f"must delete both rows for the repo, got {removed}"

    remaining = await list_user_skills_for_agent(
        db_session, tenant_id=tenant.id, principal_id=p1, agent_name="agent"
    )
    assert [r.name for r in remaining] == ["keeper"], "the other repo's row must survive"
