"""Operator identity linking refuses used routines and preserves every other field."""

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core._models import Routine
from daimon.core.errors import StoreError
from daimon.core.stores.routines import create_routine, get_routine, link_internal_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import update

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


async def setup(session, *, tenant_id=None, internal_id=None):
    if tenant_id is None:
        tenant_id = (await make_tenant(session)).id
    return await create_routine(
        session,
        tenant_id=tenant_id,
        created_by_user_id="owner",
        internal_routine_id=internal_id,
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 * * * *",
        timezone_="UTC",
        trigger_message="run",
        enabled=False,
        next_fire_at=NOW,
    )


async def test_preview_then_link_changes_only_internal_id(db_session):
    row = await setup(db_session)
    internal_id = uuid.uuid4()
    for apply in (False, True):
        await link_internal_routine(
            db_session,
            tenant_id=row.tenant_id,
            routine_id=row.id,
            internal_routine_id=internal_id,
            apply=apply,
        )
        stored = await get_routine(db_session, row.id, tenant_id=row.tenant_id)
        expected = row.model_copy(update={"internal_routine_id": internal_id}) if apply else row
        assert stored == expected


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("enabled", True, "enabled"),
        ("last_fired_at", NOW, "execution or delivery"),
        ("last_fired_slot", NOW, "execution or delivery"),
        ("last_finished_at", NOW, "execution or delivery"),
        ("last_finished_claim_at", NOW, "execution or delivery"),
        ("last_skipped_from", NOW, "execution or delivery"),
        ("last_skipped_until", NOW, "execution or delivery"),
        ("last_skip_reason", "stale", "execution or delivery"),
        ("last_error", "", "execution or delivery"),
        ("last_result_tail", "", "execution or delivery"),
        ("delivery_status", "skipped", "execution or delivery"),
        ("delivery_status", "pending", "execution or delivery"),
        ("delivery_lease_owner", "worker", "execution or delivery"),
        ("delivery_lease_expires_at", NOW, "execution or delivery"),
        ("delivery_note", "", "execution or delivery"),
        ("delivery_payload", "", "execution or delivery"),
        ("delivered_at", NOW, "execution or delivery"),
    ],
)
async def test_refuses_execution_and_delivery_state(db_session, apply, field, value, reason):
    row = await setup(db_session)
    await db_session.execute(update(Routine).where(Routine.id == row.id).values({field: value}))
    before = await get_routine(db_session, row.id, tenant_id=row.tenant_id)
    with pytest.raises(StoreError, match=reason):
        await link_internal_routine(
            db_session,
            tenant_id=row.tenant_id,
            routine_id=row.id,
            internal_routine_id=uuid.uuid4(),
            apply=apply,
        )
    assert await get_routine(db_session, row.id, tenant_id=row.tenant_id) == before


@pytest.mark.parametrize("invalid", ["missing", "cross-tenant", "same", "different", "duplicate"])
@pytest.mark.parametrize("apply", [False, True])
async def test_identity_refusals_write_nothing(db_session, invalid, apply):
    internal_id = uuid.uuid4()
    row = await setup(
        db_session, internal_id=internal_id if invalid in ("same", "different") else None
    )
    tenant_id, routine_id = row.tenant_id, row.id
    if invalid == "missing":
        routine_id = uuid.uuid4()
    elif invalid == "cross-tenant":
        tenant_id = (await make_tenant(db_session)).id
    elif invalid == "different":
        internal_id = uuid.uuid4()
    elif invalid == "duplicate":
        other = await setup(db_session, tenant_id=tenant_id, internal_id=internal_id)
    reason = {
        "missing": "does not exist in the selected team",
        "cross-tenant": "does not exist in the selected team",
        "same": f"same internal id {internal_id}",
        "different": f"different internal id {row.internal_routine_id}",
        "duplicate": "already linked to routine",
    }[invalid]
    with pytest.raises(StoreError, match=reason) as error:
        await link_internal_routine(
            db_session,
            tenant_id=tenant_id,
            routine_id=routine_id,
            internal_routine_id=internal_id,
            apply=apply,
        )
    assert await get_routine(db_session, row.id, tenant_id=row.tenant_id) == row
    if invalid == "duplicate":
        assert str(other.id) in str(error.value)
        assert await get_routine(db_session, other.id, tenant_id=tenant_id) == other


async def test_internal_identity_is_tenant_scoped(db_session):
    internal_id = uuid.uuid4()
    await setup(db_session, internal_id=internal_id)
    row = await setup(db_session)
    await link_internal_routine(
        db_session,
        tenant_id=row.tenant_id,
        routine_id=row.id,
        internal_routine_id=internal_id,
        apply=True,
    )
    assert (
        await get_routine(db_session, row.id, tenant_id=row.tenant_id)
    ).internal_routine_id == internal_id


async def test_unique_constraint_race_is_a_clear_refusal(
    db_committing_engine, db_clean, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy.sql.dml import Update

    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    internal_id = uuid.uuid4()
    async with factory.begin() as session:
        row = await setup(session)
    async with factory.begin() as session:
        execute = session.execute

        async def competing_insert(statement, *args, **kwargs):
            if isinstance(statement, Update):
                # Simulate an importer that does not participate in advisory locking.
                async with factory.begin() as rival:
                    await setup(rival, tenant_id=row.tenant_id, internal_id=internal_id)
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", competing_insert)
        with pytest.raises(StoreError, match="linked to another routine.*concurrently"):
            await link_internal_routine(
                session,
                tenant_id=row.tenant_id,
                routine_id=row.id,
                internal_routine_id=internal_id,
                apply=True,
            )
        assert await get_routine(session, row.id, tenant_id=row.tenant_id) == row
    async with factory() as session:
        assert await get_routine(session, row.id, tenant_id=row.tenant_id) == row


@pytest.mark.parametrize("archived", [False, True])
async def test_refuses_missing_or_archived_team(db_session, archived):
    from daimon.core._models import Tenant

    row = await setup(db_session)
    tenant_id = uuid.uuid4()
    if archived:
        tenant_id = row.tenant_id
        await db_session.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(archived_at=NOW)
        )
    with pytest.raises(StoreError, match="Select an active team"):
        await link_internal_routine(
            db_session,
            tenant_id=tenant_id,
            routine_id=row.id,
            internal_routine_id=uuid.uuid4(),
            apply=True,
        )
    assert await get_routine(db_session, row.id, tenant_id=row.tenant_id) == row


async def test_linked_routine_can_be_seeded(db_session):
    from daimon.core.routine_move import RoutineSeedEntry
    from daimon.core.stores.routine_move import seed_routines

    row = await setup(db_session)
    internal_id = uuid.uuid4()
    await link_internal_routine(
        db_session,
        tenant_id=row.tenant_id,
        routine_id=row.id,
        internal_routine_id=internal_id,
        apply=True,
    )
    entry = RoutineSeedEntry.model_validate(
        dict(
            internal_routine_id=internal_id,
            last_completed_slot=NOW.isoformat(),
            internal_next_run=NOW.replace(hour=13).isoformat(),
            cron_expr=row.cron_expr,
            timezone=row.timezone,
        )
    )
    await seed_routines(db_session, [entry], tenant_id=row.tenant_id, now=NOW, apply=True)
    stored = await get_routine(db_session, row.id, tenant_id=row.tenant_id)
    assert stored.enabled
    assert stored.next_fire_at == entry.internal_next_run
    assert stored.seeded_after == NOW
