"""Fresh column reads on the owned test database, without ORM side effects."""

from decimal import Decimal
from uuid import uuid4

import pytest
from daimon.core._models import Account, ProviderBindingSlot, TenantLedger
from daimon.core._models import ProviderBinding as BindingRow
from daimon.core.stores import UnflushedReadState, mux_state, read_persisted_columns
from mux.state.store import binding_slot
from sqlalchemy import event, exists, select, text, update
from sqlalchemy.orm import aliased

from ..native_usage_support import seed_binding, seed_ledger
from ..native_usage_support import sm as sm


def no_flush(db):
    def forbid(*args):
        raise RuntimeError("persisted read triggered an ORM flush")

    event.listen(db.sync_session, "before_flush", forbid)


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("readonly", [False, True])
async def test_fresh_columns_refuse_effective_source_changes(sm, deleted, autoflush, readonly):
    run = await seed_ledger(sm)
    async with sm() as db, db.begin():
        if readonly:
            await db.execute(text("SET TRANSACTION READ ONLY"))
        cached = await db.get(TenantLedger, run.id)
        if cached is None:
            raise RuntimeError("test requires a persisted source")
        if deleted:
            await db.delete(cached)
        else:
            cached.delta_usd = Decimal(1)
        db.autoflush = autoflush
        no_flush(db)
        with pytest.raises(UnflushedReadState, match="unflushed (update|delete) on tenant_ledger"):
            await read_persisted_columns(db, select(TenantLedger).where(TenantLedger.id == run.id))
        if cached not in (db.deleted if deleted else db.dirty) or db.autoflush is not autoflush:
            raise RuntimeError("read changed caller state")
        await db.rollback()
    async with sm.begin() as db:
        if await db.scalar(select(TenantLedger.delta_usd).where(TenantLedger.id == run.id)) != 0:
            raise RuntimeError("read mutated its persisted source")


@pytest.mark.parametrize("state", ["pending", "reverted", "unrelated_dirty", "unrelated_deleted"])
async def test_fresh_columns_preserve_harmless_session_state(sm, state):
    run = await seed_ledger(sm)
    async with sm() as db, db.begin():
        await db.execute(text("SET TRANSACTION READ ONLY"))
        cached = await db.get(TenantLedger, run.id)
        if cached is None:
            raise RuntimeError("test requires a persisted source")
        if state == "pending":
            pending = TenantLedger(id=uuid4(), tenant_id=run.tenant_id)
            db.add(pending)
        elif state == "reverted":
            amount = cached.delta_usd
            cached.delta_usd = Decimal(1)
            cached.delta_usd = amount
        else:
            other = await db.scalar(select(Account).where(Account.tenant_id == run.tenant_id))
            if other is None:
                raise RuntimeError("test requires an unrelated persisted source")
            if state == "unrelated_deleted":
                await db.delete(other)
            else:
                other.role = "admin"
        before = (set(db.new), set(db.dirty), set(db.deleted))
        no_flush(db)
        row = (
            await read_persisted_columns(db, select(TenantLedger).where(TenantLedger.id == run.id))
        ).one()
        if row.delta_usd != 0 or before != (set(db.new), set(db.dirty), set(db.deleted)):
            raise RuntimeError("read changed caller state or returned cached values")
        await db.rollback()


@pytest.mark.parametrize("shape", ["exists", "join", "alias"])
async def test_fresh_columns_guard_source_tables_in_nested_queries(sm, shape):
    run = await seed_ledger(sm)
    async with sm() as db, db.begin():
        await db.execute(text("SET TRANSACTION READ ONLY"))
        cached = await db.get(TenantLedger, run.id)
        if cached is None:
            raise RuntimeError("test requires a persisted source")
        cached.delta_usd = Decimal(1)
        if shape == "exists":
            query = select(Account.id).where(
                exists(select(TenantLedger.id).where(TenantLedger.id == run.id))
            )
        elif shape == "join":
            query = select(Account.id).join(
                TenantLedger, TenantLedger.tenant_id == Account.tenant_id
            )
        else:
            source = aliased(TenantLedger)
            query = select(source).where(source.id == run.id)
        no_flush(db)
        with pytest.raises(UnflushedReadState, match="update on tenant_ledger"):
            await read_persisted_columns(db, query)
        await db.rollback()


@pytest.mark.parametrize("external", [False, True])
async def test_fresh_columns_bypass_clean_cache_after_core_update(sm, external):
    run = await seed_ledger(sm)
    async with sm() as db:
        async with db.begin():
            cached = await db.get(TenantLedger, run.id)
            if cached is None:
                raise RuntimeError("test requires a persisted source")
            if not external:
                await db.execute(
                    text("UPDATE tenant_ledger SET delta_usd=2 WHERE id=:id"), {"id": run.id}
                )
                no_flush(db)
                row = (
                    await read_persisted_columns(
                        db, select(TenantLedger).where(TenantLedger.id == run.id)
                    )
                ).one()
                if row.delta_usd != 2 or cached.delta_usd != 0 or db.dirty:
                    raise RuntimeError("read used or overwrote the stale identity map")
                await db.rollback()
                return
        async with sm.begin() as writer:
            await writer.execute(
                text("UPDATE tenant_ledger SET delta_usd=2 WHERE id=:id"), {"id": run.id}
            )
        async with db.begin():
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            no_flush(db)
            row = (
                await read_persisted_columns(
                    db, select(TenantLedger).where(TenantLedger.id == run.id)
                )
            ).one()
            if row.delta_usd != 2 or cached.delta_usd != 0 or db.dirty:
                raise RuntimeError("read used or overwrote a prior transaction's cached source")


@pytest.mark.parametrize("source", ["slot", "binding"])
@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("readonly", [False, True])
async def test_persisted_binding_refuses_effective_source_changes(
    sm, source, deleted, autoflush, readonly
):
    run = await seed_ledger(sm)
    binding = await seed_binding(sm, tenant_id=run.tenant_id)
    slot = binding_slot(binding)
    async with sm() as db, db.begin():
        if readonly:
            await db.execute(text("SET TRANSACTION READ ONLY"))
        cached = (
            await db.get(ProviderBindingSlot, binding.id)
            if source == "slot"
            else await db.get(BindingRow, (binding.id, binding.generation))
        )
        if cached is None:
            raise RuntimeError("test requires a persisted binding source")
        if deleted:
            await db.delete(cached)
        else:
            cached.generation += 1
        db.autoflush = autoflush
        no_flush(db)
        table = "provider_binding_slot" if source == "slot" else "provider_binding"
        with pytest.raises(UnflushedReadState, match=f"unflushed (update|delete) on {table}"):
            await mux_state.get_binding(db, slot, read_persisted=True)
        if cached not in (db.deleted if deleted else db.dirty) or db.autoflush is not autoflush:
            raise RuntimeError("read changed caller state")
        await db.rollback()
    async with sm.begin() as db:
        if await mux_state.get_binding(db, slot) != binding:
            raise RuntimeError("binding refusal changed the persisted binding")


@pytest.mark.parametrize("source", ["slot", "binding"])
async def test_persisted_binding_reads_core_updates_without_overwriting_clean_cache(sm, source):
    run = await seed_ledger(sm)
    binding = await seed_binding(sm, tenant_id=run.tenant_id)
    slot = binding_slot(binding)
    successor = binding.model_copy(
        update={"generation": 2, "native_refs": {"session": "replacement-session"}}
    )
    if source == "slot":
        async with sm.begin() as writer:
            await mux_state.put_binding(writer, successor, expected_generation=1)
            await writer.execute(
                update(ProviderBindingSlot)
                .where(ProviderBindingSlot.binding_id == binding.id)
                .values(generation=1)
            )
    async with sm() as db, db.begin():
        if source == "slot":
            cached = await db.get(ProviderBindingSlot, binding.id)
            statement = (
                update(ProviderBindingSlot)
                .where(ProviderBindingSlot.binding_id == binding.id)
                .values(generation=2)
            )
            expected = successor
        else:
            cached = await db.get(BindingRow, (binding.id, binding.generation))
            expected = binding.model_copy(
                update={"native_refs": {"session": "replacement-session"}}
            )
            statement = (
                update(BindingRow)
                .where(BindingRow.binding_id == binding.id, BindingRow.generation == 1)
                .values(binding=expected.model_dump(mode="json"))
            )
        if cached is None:
            raise RuntimeError("test requires a cached binding source")
        await db.execute(statement.execution_options(synchronize_session=False))
        no_flush(db)
        if await mux_state.get_binding(db, slot, read_persisted=True) != expected:
            raise RuntimeError("binding read returned stale cached values")
        if cached.generation != 1 or db.dirty:
            raise RuntimeError("binding read changed caller cache")
        if source == "binding" and cached.binding != binding.model_dump(mode="json"):
            raise RuntimeError("binding read overwrote caller JSON")
        await db.rollback()
