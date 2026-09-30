"""Real-DB behavior tests for the agent_files store."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from daimon.core._models import AgentFile
from daimon.core.errors import StoreError
from daimon.core.stores.agent_files import (
    delete_agent_file,
    get_agent_file,
    list_agent_files,
    put_agent_file,
    put_agent_file_if_unchanged,
)
from daimon.core.stores.domain import AgentFileRow
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_put_agent_file_inserts_new_row_when_key_unseen(
    db_session: AsyncSession,
) -> None:
    """AF-01: first put creates a row with both timestamps populated."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="AGENT_MD",
        content="hello",
        set_by_account_id=None,
    )

    row = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="AGENT_MD")
    assert row is not None, "row should exist after put"
    assert row.content == "hello", "content should round-trip"
    assert row.created_at is not None, "created_at should be set by server_default"
    assert row.updated_at is not None, "updated_at should be set by server_default"


@pytest.mark.asyncio
async def test_put_agent_file_upserts_and_bumps_updated_at_when_key_exists(
    db_session: AsyncSession,
) -> None:
    """AF-02: second put on the same key overwrites and advances updated_at."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="k",
        content="v1",
        set_by_account_id=None,
    )
    first = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k")
    assert first is not None

    second = await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="k",
        content="v2",
        set_by_account_id=None,
    )
    assert second.content == "v2", "upsert return should reflect overwritten content"
    assert second.updated_at >= first.updated_at, (
        "updated_at must advance (or equal) on upsert; func.now() in set_ enforces this"
    )

    # Ground-truth cross-check: re-fetch with populate_existing=True (bypasses
    # the identity map) and assert the put_agent_file return matches the actual
    # DB row, not just a stale identity-map snapshot from the first put. This
    # mirrors RB-02 and guards against the CR-01 bug class regressing here.
    ground_truth = await db_session.get(
        AgentFile,
        (tenant.id, agent_id, "k"),
        populate_existing=True,
    )
    assert ground_truth is not None, "row must exist after upsert"
    assert second.content != ground_truth.content, (
        "the stored value must be ciphertext while the returned value is plaintext"
    )
    assert second.updated_at == ground_truth.updated_at, (
        "put_agent_file return must reflect the DB row for updated_at"
    )


@pytest.mark.asyncio
async def test_put_agent_file_raises_store_error_when_key_is_empty(
    db_session: AsyncSession,
) -> None:
    """AF-03: empty key validator rejects with StoreError."""
    tenant = await make_tenant(db_session)
    with pytest.raises(StoreError, match="empty"):
        await put_agent_file(
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            key="",
            content="x",
            set_by_account_id=None,
        )


@pytest.mark.asyncio
async def test_get_agent_file_returns_pydantic_row_when_present(
    db_session: AsyncSession,
) -> None:
    """AF-04: get returns AgentFileRow (Pydantic), not the ORM AgentFile."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="k",
        content="v",
        set_by_account_id=None,
    )
    row = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k")
    assert isinstance(row, AgentFileRow), "store must return Pydantic, not ORM"
    assert not isinstance(row, AgentFile), "ORM must not leak past the store boundary"


@pytest.mark.asyncio
async def test_get_agent_file_returns_none_when_missing(
    db_session: AsyncSession,
) -> None:
    """AF-05: get on a missing key returns None."""
    tenant = await make_tenant(db_session)
    row = await get_agent_file(
        db_session, tenant_id=tenant.id, agent_id=uuid.uuid4(), key="missing"
    )
    assert row is None, "missing row should return None, not raise"


@pytest.mark.asyncio
async def test_list_agent_files_returns_keys_ordered_when_multiple_present(
    db_session: AsyncSession,
) -> None:
    """AF-06: list returns rows ordered by key ascending."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    for k in ["b", "a", "c"]:
        await put_agent_file(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            key=k,
            content=k,
            set_by_account_id=None,
        )
    rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert [r.key for r in rows] == ["a", "b", "c"], "list should be ordered by key"


@pytest.mark.asyncio
async def test_list_agent_files_scopes_to_tenant_and_agent(
    db_session: AsyncSession,
) -> None:
    """AF-07: list does not leak across tenants or agents."""
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    a1 = uuid.uuid4()
    a2 = uuid.uuid4()
    await put_agent_file(
        db_session, tenant_id=t1.id, agent_id=a1, key="k1", content="x", set_by_account_id=None
    )
    await put_agent_file(
        db_session, tenant_id=t1.id, agent_id=a2, key="k2", content="x", set_by_account_id=None
    )
    await put_agent_file(
        db_session, tenant_id=t2.id, agent_id=a1, key="k3", content="x", set_by_account_id=None
    )

    rows = await list_agent_files(db_session, tenant_id=t1.id, agent_id=a1)
    assert [r.key for r in rows] == ["k1"], (
        "list must filter by both tenant_id and agent_id; cross-tenant/agent rows leaked"
    )


@pytest.mark.asyncio
async def test_delete_agent_file_removes_row_when_present(
    db_session: AsyncSession,
) -> None:
    """AF-08: delete removes the row; subsequent get returns None."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="k",
        content="v",
        set_by_account_id=None,
    )
    await delete_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k")
    row = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k")
    assert row is None, "row should be gone after delete"


@pytest.mark.asyncio
async def test_tenant_delete_cascades_to_agent_files(
    db_session: AsyncSession,
) -> None:
    """AF-09: deleting the tenant removes its agent_files via FK CASCADE."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="k",
        content="v",
        set_by_account_id=None,
    )

    await db_session.execute(sa.text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant.id})
    await db_session.flush()

    rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert rows == [], "tenant FK CASCADE should have removed agent_files rows"


@pytest.mark.asyncio
async def test_put_agent_file_records_creator_on_insert_and_setter_on_update(
    db_session: AsyncSession,
) -> None:
    """The creator of a key survives every later replacement of its value."""
    tenant = await make_tenant(db_session)
    creator = await make_account(db_session, tenant=tenant)
    replacer = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()

    created = await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="TOGGL_TOKEN",
        content="v1",
        set_by_account_id=creator.id,
    )
    assert created.created_by_account_id == creator.id, "insert must record the creator"
    assert created.last_set_by_account_id == creator.id, "insert must also record the setter"

    replaced = await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="TOGGL_TOKEN",
        content="v2",
        set_by_account_id=replacer.id,
    )
    assert replaced.created_by_account_id == creator.id, (
        "a replacement must not rewrite the creator — the card names whose value was overwritten"
    )
    assert replaced.last_set_by_account_id == replacer.id, (
        "a replacement must record the account that replaced the value"
    )


@pytest.mark.asyncio
async def test_put_agent_file_if_unchanged_inserts_when_key_absent(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()

    row = await put_agent_file_if_unchanged(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="NEW_KEY",
        content="v1",
        set_by_account_id=account.id,
        expected_updated_at=None,
    )

    assert row is not None, "expected_updated_at=None with no existing key must insert"
    assert row.content == "v1", "the inserted content must round-trip"
    assert row.created_by_account_id == account.id, "the insert must record the creator"


@pytest.mark.asyncio
async def test_put_agent_file_if_unchanged_writes_when_updated_at_matches(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    before = await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="v1",
        set_by_account_id=None,
    )

    row = await put_agent_file_if_unchanged(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="v2",
        set_by_account_id=account.id,
        expected_updated_at=before.updated_at,
    )

    assert row is not None, "the precondition held, so the write must land"
    assert row.content == "v2", "the new content must be stored"
    assert row.last_set_by_account_id == account.id, "the writer must be recorded"


@pytest.mark.asyncio
async def test_put_agent_file_if_unchanged_returns_none_when_row_changed_since(
    db_session: AsyncSession,
) -> None:
    """A stale card must not clobber the value that replaced the one it saw."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    stale = await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="v1",
        set_by_account_id=None,
    )
    # Separate transactions: `updated_at` is `now()`, which is transaction
    # time, so two writes inside one transaction would share a timestamp — and
    # a real race is between transactions anyway.
    await db_session.commit()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="v2-someone-else",
        set_by_account_id=None,
    )

    row = await put_agent_file_if_unchanged(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="v3-from-the-stale-card",
        set_by_account_id=None,
        expected_updated_at=stale.updated_at,
    )

    assert row is None, "a failed precondition must return None, not a row"
    stored = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="K")
    assert stored is not None, "the key must still exist"
    assert stored.content == "v2-someone-else", (
        "the later write must survive — the stale card's content must never land"
    )


@pytest.mark.asyncio
async def test_put_agent_file_if_unchanged_returns_none_when_key_appeared_since(
    db_session: AsyncSession,
) -> None:
    """The card promised the key did not exist; someone created it first."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="created-by-someone-else",
        set_by_account_id=None,
    )

    row = await put_agent_file_if_unchanged(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="from-the-card",
        set_by_account_id=None,
        expected_updated_at=None,
    )

    assert row is None, "expected_updated_at=None against an existing key is a failed precondition"
    stored = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="K")
    assert stored is not None, "the pre-existing key must still exist"
    assert stored.content == "created-by-someone-else", "the existing value must be untouched"


@pytest.mark.asyncio
async def test_put_agent_file_if_unchanged_returns_none_when_key_removed_since(
    db_session: AsyncSession,
) -> None:
    """Removed and overwritten are the same failure: the card's promise no longer holds."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    before = await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="v1",
        set_by_account_id=None,
    )
    await delete_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="K")

    row = await put_agent_file_if_unchanged(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        key="K",
        content="v2",
        set_by_account_id=None,
        expected_updated_at=before.updated_at,
    )

    assert row is None, "a removed key must fail the precondition rather than be re-created"
    stored = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="K")
    assert stored is None, "the key must stay removed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["LD_PRELOAD", "BASH_ENV", "PATH", "GIT_CONFIG_KEY_0", "https_proxy", "X;id;Y", "notes.md"],
)
async def test_put_agent_file_refuses_a_key_the_sandbox_would_export_unsafely(
    db_session: AsyncSession, key: str
) -> None:
    """Every write path funnels here, so a reserved or non-identifier name never lands."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    with pytest.raises(StoreError):
        await put_agent_file(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            key=key,
            content="/tmp/evil.so",
            set_by_account_id=None,
        )
    with pytest.raises(StoreError):
        await put_agent_file_if_unchanged(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            key=key,
            content="/tmp/evil.so",
            set_by_account_id=None,
            expected_updated_at=None,
        )
    assert await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent_id) == []


@pytest.mark.asyncio
async def test_put_agent_file_refuses_a_nul_without_echoing_the_value(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    with pytest.raises(StoreError) as caught:
        await put_agent_file(
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            key="API_KEY",
            content="sec\0ret",
            set_by_account_id=None,
        )
    assert "sec" not in str(caught.value), "the error must not carry the value"
