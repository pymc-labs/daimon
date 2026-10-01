"""Real-DB behavior tests for the agent_repo_binding store."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from daimon.core._models import AgentRepoBinding
from daimon.core.errors import StoreError
from daimon.core.stores.agent_repo_binding import (
    clear_binding,
    get_binding,
    get_bindings_for_repo,
    list_bindings_without_proof,
    record_proof,
    set_binding,
    update_last_sync,
    update_repo_and_branch_keep_secret,
)
from daimon.core.stores.domain import AgentRepoBindingRow, RepoAccessProof
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_set_binding_inserts_new_row(db_session: AsyncSession) -> None:
    """RB-01: first set creates a binding with both timestamps populated."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    row = await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/repo.git",
        default_branch="main",
        ma_secret_ref="secret-ref-1",
        proof=None,
    )

    assert isinstance(row, AgentRepoBindingRow), "set_binding must return Pydantic, not ORM"
    assert not isinstance(row, AgentRepoBinding), "ORM must not leak past the store boundary"
    assert row.repo_url == "example/repo", "set_binding normalizes repo_url to 'owner/repo' form"
    assert row.default_branch == "main", "default_branch should round-trip"
    assert row.ma_secret_ref == "secret-ref-1", "ma_secret_ref should round-trip"
    assert row.created_at is not None, "created_at should be set by server_default"
    assert row.updated_at is not None, "updated_at should be set by server_default"


@pytest.mark.asyncio
async def test_set_binding_overwrites_existing(db_session: AsyncSession) -> None:
    """RB-02: second set_binding on same (tenant, agent) overwrites and advances updated_at."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    first = await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/old.git",
        default_branch="main",
        ma_secret_ref="secret-old",
        proof=None,
    )

    second = await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/new.git",
        default_branch="develop",
        ma_secret_ref="secret-new",
        proof=None,
    )

    assert second.repo_url == "example/new", "upsert must overwrite repo_url (normalized form)"
    assert second.default_branch == "develop", "upsert must overwrite default_branch"
    assert second.ma_secret_ref == "secret-new", "upsert must overwrite ma_secret_ref"
    assert second.updated_at >= first.updated_at, (
        "updated_at must advance (or equal) on upsert; func.now() in set_ enforces this"
    )

    # Confirm 1:1 — only one row exists for this (tenant, agent).
    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AgentRepoBinding)
        .where(
            AgentRepoBinding.tenant_id == tenant.id,
            AgentRepoBinding.agent_id == agent_id,
        )
    )
    assert count == 1, "1:1 cardinality: only one binding row per (tenant, agent)"

    # Ground-truth cross-check: re-fetch with populate_existing=True (bypasses
    # the identity map) and assert the set_binding return matches the actual
    # DB row, not just the input args. This is the assertion that would have
    # caught CR-01 — without populate_existing, the cached ORM instance from
    # the first set's re-read could mask a stale return value from the second.
    ground_truth = await db_session.get(
        AgentRepoBinding,
        (tenant.id, agent_id),
        populate_existing=True,
    )
    assert ground_truth is not None, "row must exist after upsert"
    assert second.repo_url == ground_truth.repo_url, (
        "set_binding return must reflect the DB row, not a stale identity-map snapshot"
    )
    assert second.default_branch == ground_truth.default_branch, (
        "set_binding return must reflect the DB row for default_branch"
    )
    assert second.ma_secret_ref == ground_truth.ma_secret_ref, (
        "set_binding return must reflect the DB row for ma_secret_ref"
    )


@pytest.mark.asyncio
async def test_get_binding_returns_row_when_bound(db_session: AsyncSession) -> None:
    """RB-03: get returns AgentRepoBindingRow (Pydantic) when a binding exists."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/repo.git",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=None,
    )

    row = await get_binding(db_session, tenant_id=tenant.id, agent_id=agent_id)

    assert isinstance(row, AgentRepoBindingRow), "get_binding must return Pydantic, not ORM"
    assert not isinstance(row, AgentRepoBinding), "ORM must not leak past the store boundary"
    assert row.repo_url == "example/repo", (
        "get_binding returns normalized repo_url (stored via set_binding normalization)"
    )


@pytest.mark.asyncio
async def test_get_binding_returns_none_when_unbound(db_session: AsyncSession) -> None:
    """RB-04: get returns None when no binding exists."""
    tenant = await make_tenant(db_session)

    row = await get_binding(db_session, tenant_id=tenant.id, agent_id=uuid.uuid4())

    assert row is None, "missing binding should return None, not raise"


@pytest.mark.asyncio
async def test_clear_binding_removes_row(db_session: AsyncSession) -> None:
    """RB-05: clear removes the row; subsequent get returns None."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/repo.git",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=None,
    )

    await clear_binding(db_session, tenant_id=tenant.id, agent_id=agent_id)

    row = await get_binding(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert row is None, "binding should be gone after clear"


@pytest.mark.asyncio
async def test_clear_binding_raises_storeerror_when_unbound(
    db_session: AsyncSession,
) -> None:
    """RB-06: clear on a missing binding raises StoreError("no binding for agent ...")."""
    tenant = await make_tenant(db_session)

    with pytest.raises(StoreError, match="no binding for agent"):
        await clear_binding(db_session, tenant_id=tenant.id, agent_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_tenant_cascade_deletes_binding(db_session: AsyncSession) -> None:
    """RB-07: deleting the tenant removes its agent_repo_binding via FK CASCADE."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/repo.git",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=None,
    )

    await db_session.execute(sa.text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant.id})
    await db_session.flush()

    row = await get_binding(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert row is None, "tenant FK CASCADE should have removed the binding row"


@pytest.mark.asyncio
async def test_cross_tenant_isolation(db_session: AsyncSession) -> None:
    """RB-08: a binding in tenant A is not visible from tenant B."""
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    agent_id = uuid.uuid4()  # same agent_id across tenants

    await set_binding(
        db_session,
        tenant_id=t1.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/t1.git",
        default_branch="main",
        ma_secret_ref="secret-t1",
        proof=None,
    )

    # Same agent_id under tenant 2 must not see tenant 1's binding.
    row_from_t2 = await get_binding(db_session, tenant_id=t2.id, agent_id=agent_id)
    assert row_from_t2 is None, "tenant B must not see tenant A's binding"

    # And tenant 1 still sees its own.
    row_from_t1 = await get_binding(db_session, tenant_id=t1.id, agent_id=agent_id)
    assert row_from_t1 is not None, "tenant A must still see its own binding"
    assert row_from_t1.repo_url == "example/t1", (
        "tenant A's binding should be intact (stored in normalized form)"
    )


# ---------------------------------------------------------------------------
# Normalization unification + reverse lookup + last_sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_binding_normalizes_repo_url(db_session: AsyncSession) -> None:
    """RB-09: set_binding stores the canonical 'owner/repo' form, not the raw URL."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    row = await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/owner/repo.git",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=None,
    )

    assert row.repo_url == "owner/repo", (
        "set_binding should normalize 'https://github.com/owner/repo.git' to 'owner/repo'"
    )


@pytest.mark.asyncio
async def test_get_bindings_for_repo_normalizes(db_session: AsyncSession) -> None:
    """RB-10: get_bindings_for_repo matches a binding stored with a full URL via normalization.

    Proves Pitfall 2 fix: set_binding stored with a full URL and lookup with 'owner/repo'
    returns the same row (both sides normalize identically).
    """
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/owner/repo.git",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=None,
    )

    rows = await get_bindings_for_repo(db_session, repo_url="owner/repo")

    assert len(rows) == 1, "lookup by canonical 'owner/repo' should find the stored binding"
    assert isinstance(rows[0], AgentRepoBindingRow), "must return Pydantic rows"
    assert rows[0].agent_id == agent_id, "should return the binding for the correct agent"
    assert rows[0].repo_url == "owner/repo", (
        "stored repo_url should be in canonical form after normalization"
    )


@pytest.mark.asyncio
async def test_get_bindings_for_repo_returns_all_tenants(db_session: AsyncSession) -> None:
    """Get_bindings_for_repo returns bindings from all tenants (install-agnostic)."""
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    agent_id_1 = uuid.uuid4()
    agent_id_2 = uuid.uuid4()

    await set_binding(
        db_session,
        tenant_id=t1.id,
        agent_id=agent_id_1,
        repo_url="org/shared-starter",
        default_branch="main",
        ma_secret_ref="secret-t1",
        proof=None,
    )
    await set_binding(
        db_session,
        tenant_id=t2.id,
        agent_id=agent_id_2,
        repo_url="org/shared-starter",
        default_branch="main",
        ma_secret_ref="secret-t2",
        proof=None,
    )

    rows = await get_bindings_for_repo(db_session, repo_url="org/shared-starter")

    assert len(rows) == 2, "should return bindings from all tenants for the same repo"
    agent_ids = {r.agent_id for r in rows}
    assert agent_id_1 in agent_ids, "tenant 1's binding should be included"
    assert agent_id_2 in agent_ids, "tenant 2's binding should be included"


@pytest.mark.asyncio
async def test_get_bindings_for_repo_returns_empty_on_miss(db_session: AsyncSession) -> None:
    """RB-12: get_bindings_for_repo returns an empty list when no binding matches."""
    rows = await get_bindings_for_repo(db_session, repo_url="nobody/no-such-repo")

    assert rows == [], "no match should return an empty list, not raise"


@pytest.mark.asyncio
async def test_update_last_sync_persists(db_session: AsyncSession) -> None:
    """RB-13: update_last_sync persists last_sync_at and last_sync_error; get_binding reads them back."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="org/my-repo",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=None,
    )

    sync_time = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    updated = await update_last_sync(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        last_sync_at=sync_time,
        last_sync_error="tarball fetch failed: 404",
    )

    assert updated.last_sync_at is not None, "last_sync_at should be set"
    assert updated.last_sync_error == "tarball fetch failed: 404", (
        "last_sync_error should round-trip"
    )

    # Re-read via get_binding to confirm DB persistence (not just RETURNING value).
    row = await get_binding(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert row is not None, "binding should still exist after update_last_sync"
    assert row.last_sync_at is not None, "last_sync_at should persist in DB"
    assert row.last_sync_error == "tarball fetch failed: 404", (
        "last_sync_error should persist in DB"
    )


@pytest.mark.asyncio
async def test_update_last_sync_clears_error(db_session: AsyncSession) -> None:
    """RB-14: update_last_sync with last_sync_error=None clears a previous error."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="org/my-repo",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=None,
    )

    # Set an error first.
    await update_last_sync(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        last_sync_at=datetime(2026, 5, 30, 11, 0, 0, tzinfo=UTC),
        last_sync_error="some transient error",
    )

    # Then clear it on success.
    cleared = await update_last_sync(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        last_sync_at=datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC),
        last_sync_error=None,
    )

    assert cleared.last_sync_error is None, (
        "update_last_sync with None should clear the previous error"
    )


@pytest.mark.asyncio
async def test_update_last_sync_raises_when_no_binding(db_session: AsyncSession) -> None:
    """RB-15: update_last_sync raises StoreError when no binding exists."""
    tenant = await make_tenant(db_session)

    with pytest.raises(StoreError, match="no binding for agent"):
        await update_last_sync(
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            last_sync_at=datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC),
            last_sync_error=None,
        )


# ---------------------------------------------------------------------------
# update_repo_and_branch_keep_secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_repo_and_branch_keep_secret_preserves_ma_secret_ref(
    db_session: AsyncSession,
) -> None:
    """RB-16: the keep-secret helper updates repo_url/default_branch but leaves ma_secret_ref alone."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/old.git",
        default_branch="main",
        ma_secret_ref=f"inline-pat:{agent_id}",
        proof=None,
    )

    updated = await update_repo_and_branch_keep_secret(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/example/new.git",
        default_branch="develop",
    )

    assert updated.repo_url == "example/new", "repo_url should be updated (normalized form)"
    assert updated.default_branch == "develop", "default_branch should be updated"
    assert updated.ma_secret_ref == f"inline-pat:{agent_id}", (
        "ma_secret_ref must be preserved — the keep-secret helper must never write it"
    )

    # Re-read to confirm DB persistence, not just the RETURNING value.
    row = await get_binding(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert row is not None
    assert row.ma_secret_ref == f"inline-pat:{agent_id}", (
        "ma_secret_ref must remain inline-pat:{agent_id} in the DB after the keep-secret update"
    )


@pytest.mark.asyncio
async def test_update_repo_and_branch_keep_secret_normalizes_repo_url(
    db_session: AsyncSession,
) -> None:
    """RB-17: the keep-secret helper normalizes repo_url the same way set_binding does."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/old-repo",
        default_branch="main",
        ma_secret_ref="anon:",
        proof=None,
    )

    updated = await update_repo_and_branch_keep_secret(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/owner/new-repo.git",
        default_branch="main",
    )

    assert updated.repo_url == "owner/new-repo", (
        "keep-secret helper should normalize 'https://github.com/owner/new-repo.git' to 'owner/new-repo'"
    )


@pytest.mark.asyncio
async def test_update_repo_and_branch_keep_secret_raises_when_no_binding(
    db_session: AsyncSession,
) -> None:
    """RB-18: the keep-secret helper raises StoreError when no binding exists (mirrors update_last_sync)."""
    tenant = await make_tenant(db_session)

    with pytest.raises(StoreError, match="no binding for agent"):
        await update_repo_and_branch_keep_secret(
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            repo_url="owner/repo",
            default_branch="main",
        )


# ---------------------------------------------------------------------------
# Proof-of-access: set_binding, keep-secret repo edit,
# list_bindings_without_proof, record_proof
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_binding_with_proof_persists_all_three_proof_columns(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    proof = RepoAccessProof(
        kind="pat", at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC), account_id=account.id
    )

    row = await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=proof,
    )

    assert row.proof_kind == "pat", "set_binding with a proof should persist proof_kind"
    assert row.proof_at == proof.at, "set_binding with a proof should persist proof_at"
    assert row.proof_account_id == account.id, (
        "set_binding with a proof should persist proof_account_id"
    )


@pytest.mark.asyncio
async def test_set_binding_without_proof_on_existing_proven_row_clears_proof(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    proof = RepoAccessProof(
        kind="public", at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC), account_id=account.id
    )
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=proof,
    )

    rebound = await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref="secret-ref-2",
        proof=None,
    )

    assert rebound.proof_kind is None, (
        "a re-bind with no proof must clear a stale proof_kind, not inherit it"
    )
    assert rebound.proof_at is None, "a re-bind with no proof must clear a stale proof_at"
    assert rebound.proof_account_id is None, (
        "a re-bind with no proof must clear a stale proof_account_id"
    )


@pytest.mark.asyncio
async def test_update_repo_and_branch_keep_secret_with_new_repo_nulls_proof(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    proof = RepoAccessProof(
        kind="pat", at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC), account_id=account.id
    )
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/old-repo",
        default_branch="main",
        ma_secret_ref=f"inline-pat:{agent_id}",
        proof=proof,
    )

    updated = await update_repo_and_branch_keep_secret(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/new-repo",
        default_branch="main",
    )

    assert updated.repo_url == "owner/new-repo", "repo_url should be updated"
    assert updated.proof_kind is None, "a repo-URL change must clear proof_kind"
    assert updated.proof_at is None, "a repo-URL change must clear proof_at"
    assert updated.proof_account_id is None, "a repo-URL change must clear proof_account_id"
    assert updated.ma_secret_ref == f"inline-pat:{agent_id}", (
        "ma_secret_ref must remain untouched by a repo-URL change"
    )


@pytest.mark.asyncio
async def test_update_repo_and_branch_keep_secret_with_same_repo_leaves_proof_intact(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    proof = RepoAccessProof(
        kind="pat", at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC), account_id=account.id
    )
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref=f"inline-pat:{agent_id}",
        proof=proof,
    )

    updated = await update_repo_and_branch_keep_secret(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/repo",
        default_branch="develop",
    )

    assert updated.default_branch == "develop", "default_branch should be updated"
    assert updated.proof_kind == "pat", "a branch-only edit must leave proof_kind intact"
    assert updated.proof_at == proof.at, "a branch-only edit must leave proof_at intact"
    assert updated.proof_account_id == account.id, (
        "a branch-only edit must leave proof_account_id intact"
    )


@pytest.mark.asyncio
async def test_update_repo_and_branch_keep_secret_url_differing_by_scheme_does_not_clear_proof(
    db_session: AsyncSession,
) -> None:
    """Normalization is load-bearing: a URL that normalizes to the SAME 'owner/repo'
    (differing only by scheme or a trailing '.git') counts as the same repo."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    proof = RepoAccessProof(
        kind="public", at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC), account_id=account.id
    )
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref="anon:",
        proof=proof,
    )

    updated = await update_repo_and_branch_keep_secret(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/owner/repo.git",
        default_branch="main",
    )

    assert updated.repo_url == "owner/repo", "normalized form should be unchanged"
    assert updated.proof_kind == "public", (
        "a URL differing only by scheme/.git normalizes to the same repo and must not clear proof"
    )
    assert updated.proof_account_id == account.id


@pytest.mark.asyncio
async def test_list_bindings_without_proof_returns_only_proofless_rows_across_tenants(
    db_session: AsyncSession,
) -> None:
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    account = await make_account(db_session, tenant=t1)
    proven_agent_id = uuid.uuid4()
    proofless_agent_id_1 = uuid.uuid4()
    proofless_agent_id_2 = uuid.uuid4()

    await set_binding(
        db_session,
        tenant_id=t1.id,
        agent_id=proven_agent_id,
        repo_url="owner/proven-repo",
        default_branch="main",
        ma_secret_ref="secret-proven",
        proof=RepoAccessProof(
            kind="pat", at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC), account_id=account.id
        ),
    )
    await set_binding(
        db_session,
        tenant_id=t1.id,
        agent_id=proofless_agent_id_1,
        repo_url="owner/proofless-a",
        default_branch="main",
        ma_secret_ref="secret-a",
        proof=None,
    )
    await set_binding(
        db_session,
        tenant_id=t2.id,
        agent_id=proofless_agent_id_2,
        repo_url="owner/proofless-b",
        default_branch="main",
        ma_secret_ref="secret-b",
        proof=None,
    )

    rows = await list_bindings_without_proof(db_session)

    proofless_agent_ids = {r.agent_id for r in rows}
    assert proven_agent_id not in proofless_agent_ids, (
        "a proven binding must not appear in list_bindings_without_proof"
    )
    assert proofless_agent_id_1 in proofless_agent_ids, (
        "a proofless binding in tenant 1 must appear"
    )
    assert proofless_agent_id_2 in proofless_agent_ids, (
        "a proofless binding in tenant 2 must appear (install-agnostic across tenants)"
    )


@pytest.mark.asyncio
async def test_record_proof_overwrites_proof_without_altering_repo_or_secret(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref=f"inline-pat:{agent_id}",
        proof=None,
    )

    fresh_proof = RepoAccessProof(
        kind="pat", at=datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC), account_id=account.id
    )
    updated = await record_proof(
        db_session, tenant_id=tenant.id, agent_id=agent_id, proof=fresh_proof
    )

    assert updated.proof_kind == "pat", "record_proof should stamp proof_kind"
    assert updated.proof_at == fresh_proof.at, "record_proof should stamp proof_at"
    assert updated.proof_account_id == account.id, "record_proof should stamp proof_account_id"
    assert updated.repo_url == "owner/repo", "record_proof must not alter repo_url"
    assert updated.default_branch == "main", "record_proof must not alter default_branch"
    assert updated.ma_secret_ref == f"inline-pat:{agent_id}", (
        "record_proof must not alter ma_secret_ref"
    )


@pytest.mark.asyncio
async def test_record_proof_raises_storeerror_for_unknown_agent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    with pytest.raises(StoreError, match="no binding for agent"):
        await record_proof(
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            proof=RepoAccessProof(
                kind="public",
                at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC),
                account_id=account.id,
            ),
        )


@pytest.mark.asyncio
async def test_repo_edit_then_record_proof_in_one_transaction_leaves_new_repo_and_new_proof(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    old_proof = RepoAccessProof(
        kind="pat", at=datetime(2026, 7, 31, 9, 0, 0, tzinfo=UTC), account_id=account.id
    )
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/old-repo",
        default_branch="main",
        ma_secret_ref=f"inline-pat:{agent_id}",
        proof=old_proof,
    )

    await update_repo_and_branch_keep_secret(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/new-repo",
        default_branch="main",
    )
    new_proof = RepoAccessProof(
        kind="pat", at=datetime(2026, 7, 31, 11, 0, 0, tzinfo=UTC), account_id=account.id
    )
    final = await record_proof(db_session, tenant_id=tenant.id, agent_id=agent_id, proof=new_proof)

    assert final.repo_url == "owner/new-repo", "the new repo must stick"
    assert final.proof_kind == "pat", "the freshly recorded proof must stick"
    assert final.proof_at == new_proof.at, "the freshly recorded proof's timestamp must stick"
    assert final.proof_account_id == account.id


# ---------------------------------------------------------------------------
# proof keyword is required — no default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_binding_raises_typeerror_when_proof_omitted(
    db_session: AsyncSession,
) -> None:
    """The runtime face of the compile-time contract: proof has no default,
    so calling set_binding without it is a TypeError, not a silent NULL."""
    tenant = await make_tenant(db_session)

    with pytest.raises(TypeError):
        await set_binding(  # type: ignore[call-arg]
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            repo_url="owner/repo",
            default_branch="main",
            ma_secret_ref="secret-ref",
        )


@pytest.mark.asyncio
async def test_set_binding_explicit_proof_none_writes_all_three_columns_null(
    db_session: AsyncSession,
) -> None:
    """Passing proof=None explicitly must stay writable: it is the state
    pre-migration rows carry and clearing proof on a repo change relies on."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    row = await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="owner/repo",
        default_branch="main",
        ma_secret_ref="secret-ref",
        proof=None,
    )

    assert row.proof_kind is None, "explicit proof=None must leave proof_kind NULL"
    assert row.proof_at is None, "explicit proof=None must leave proof_at NULL"
    assert row.proof_account_id is None, "explicit proof=None must leave proof_account_id NULL"
