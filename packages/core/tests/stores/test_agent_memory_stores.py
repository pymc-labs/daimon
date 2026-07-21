"""Real-DB behavior tests for the agent_memory_stores store."""

from __future__ import annotations

import uuid

import pytest
from daimon.core._models import AgentMemoryStore
from daimon.core.errors import StoreError
from daimon.core.stores.agent_memory_stores import (
    clear_memory_store,
    get_memory_store_id,
    insert_memory_store,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


async def test_get_returns_none_when_unbound(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    result = await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=uuid.uuid4())
    assert result is None


async def test_insert_then_get_roundtrip(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    won = await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_A"
    )
    assert won == "memstore_A", "first insert must win with its own id"
    got = await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert got == "memstore_A"


async def test_insert_conflict_returns_existing_id(db_session: AsyncSession) -> None:
    """Same-transaction conflict: a second insert against a row already
    committed-or-flushed in *this* session/transaction is a no-op and
    returns the existing id, not the caller's own value.

    This does NOT exercise the cross-transaction race the store exists for
    (see test_insert_conflict_across_transactions_returns_first_committed_id
    below for that) — both inserts here share one connection/transaction, so
    it only proves ON CONFLICT DO NOTHING behaves correctly within a single
    transaction.
    """
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_A"
    )
    won = await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_B"
    )
    assert won == "memstore_A", "conflict must return the existing binding, not overwrite"


async def test_insert_conflict_across_transactions_returns_first_committed_id(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """Genuine cross-transaction race: transaction A inserts+commits
    memstore_A on one connection; transaction B, on a second, independent
    connection/session, then tries to insert memstore_B for the same
    (tenant_id, agent_id) and must lose, returning "memstore_A".

    This is the scenario ON CONFLICT DO NOTHING + follow-up SELECT actually
    guards against (two concurrent first-sessions racing to provision an MA
    memory store) — same-transaction duplicate inserts (see the test above)
    can't prove that on their own.

    `db_session` pins its schema via a `SET search_path` issued on its own
    connection (daimon.testing.db.db_session); a second connection off the
    same `db_engine` does not inherit that search_path, so we discover the
    per-test schema name via `current_schema()` on `db_session` and set it
    explicitly on the second connection before using it.
    """
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    # Transaction A: insert+commit memstore_A on db_session's connection.
    won_a = await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_A"
    )
    assert won_a == "memstore_A"
    await db_session.commit()

    schema = (await db_session.execute(text("SELECT current_schema()"))).scalar_one()

    # Transaction B: a fully independent connection/session, pinned to the
    # same per-test schema, racing to insert memstore_B for the same key.
    async with db_engine.connect() as other_conn:
        await other_conn.execute(text(f'SET search_path TO "{schema}", public'))
        other_session_factory = async_sessionmaker(bind=other_conn, expire_on_commit=False)
        async with other_session_factory() as other_session:
            won_b = await insert_memory_store(
                other_session,
                tenant_id=tenant.id,
                agent_id=agent_id,
                memory_store_id="memstore_B",
            )
            await other_session.commit()

    assert won_b == "memstore_A", (
        "the transaction that committed first must win across independent "
        "connections, not just within one transaction"
    )


class _ChurnSession:
    """Delegates to a real session, but deletes the binding row just before
    the store's follow-up SELECT — the interleaving a concurrent
    clear_memory_store produces between the two statements of
    insert_memory_store."""

    def __init__(
        self, real: AsyncSession, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, churns: int
    ) -> None:
        self._real = real
        self._tenant_id = tenant_id
        self._agent_id = agent_id
        self._churns = churns

    async def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if getattr(stmt, "is_select", False) and self._churns > 0:
            self._churns -= 1
            await self._real.execute(
                delete(AgentMemoryStore).where(
                    AgentMemoryStore.tenant_id == self._tenant_id,
                    AgentMemoryStore.agent_id == self._agent_id,
                )
            )
        return await self._real.execute(stmt, *args, **kwargs)

    async def flush(self) -> None:
        await self._real.flush()


async def test_insert_retries_after_concurrent_clear(db_session: AsyncSession) -> None:
    """One concurrent clear between insert and select: the retry re-inserts
    and wins with the caller's own id instead of raising NoResultFound."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    churn = _ChurnSession(db_session, tenant_id=tenant.id, agent_id=agent_id, churns=1)
    won = await insert_memory_store(
        churn,  # type: ignore[arg-type]  # duck-typed AsyncSession
        tenant_id=tenant.id,
        agent_id=agent_id,
        memory_store_id="memstore_A",
    )
    assert won == "memstore_A"
    got = await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert got == "memstore_A"


async def test_insert_raises_store_error_when_churn_outpaces_retries(
    db_session: AsyncSession,
) -> None:
    """A clear landing between insert and select on every attempt exhausts
    the retry budget and surfaces as StoreError, not NoResultFound."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    churn = _ChurnSession(db_session, tenant_id=tenant.id, agent_id=agent_id, churns=99)
    with pytest.raises(StoreError, match="retry budget"):
        await insert_memory_store(
            churn,  # type: ignore[arg-type]  # duck-typed AsyncSession
            tenant_id=tenant.id,
            agent_id=agent_id,
            memory_store_id="memstore_A",
        )


async def test_tenant_isolation(db_session: AsyncSession) -> None:
    """Same agent_id under a different tenant is a distinct binding."""
    t1 = await make_tenant(db_session)
    t2 = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=t1.id, agent_id=agent_id, memory_store_id="memstore_T1"
    )
    assert (await get_memory_store_id(db_session, tenant_id=t2.id, agent_id=agent_id)) is None


async def test_clear_is_idempotent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await insert_memory_store(
        db_session, tenant_id=tenant.id, agent_id=agent_id, memory_store_id="memstore_A"
    )
    await clear_memory_store(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert (await get_memory_store_id(db_session, tenant_id=tenant.id, agent_id=agent_id)) is None
    # second clear must not raise
    await clear_memory_store(db_session, tenant_id=tenant.id, agent_id=agent_id)
