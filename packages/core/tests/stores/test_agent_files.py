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
)
from daimon.core.stores.domain import AgentFileRow
from daimon.testing.factories import make_tenant
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
        key="AGENT.md",
        content="hello",
    )

    row = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="AGENT.md")
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
    await put_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k", content="v1")
    first = await get_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k")
    assert first is not None

    second = await put_agent_file(
        db_session, tenant_id=tenant.id, agent_id=agent_id, key="k", content="v2"
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
    assert second.content == ground_truth.content, (
        "put_agent_file return must reflect the DB row, not a stale identity-map snapshot"
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
        )


@pytest.mark.asyncio
async def test_get_agent_file_returns_pydantic_row_when_present(
    db_session: AsyncSession,
) -> None:
    """AF-04: get returns AgentFileRow (Pydantic), not the ORM AgentFile."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await put_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k", content="v")
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
        await put_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key=k, content=k)
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
    await put_agent_file(db_session, tenant_id=t1.id, agent_id=a1, key="k1", content="x")
    await put_agent_file(db_session, tenant_id=t1.id, agent_id=a2, key="k2", content="x")
    await put_agent_file(db_session, tenant_id=t2.id, agent_id=a1, key="k3", content="x")

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
    await put_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k", content="v")
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
    await put_agent_file(db_session, tenant_id=tenant.id, agent_id=agent_id, key="k", content="v")

    await db_session.execute(sa.text("DELETE FROM tenants WHERE id = :tid"), {"tid": tenant.id})
    await db_session.flush()

    rows = await list_agent_files(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert rows == [], "tenant FK CASCADE should have removed agent_files rows"
