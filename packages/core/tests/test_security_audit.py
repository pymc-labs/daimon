import uuid
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core._models import SecurityAuditEvent
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.security_audit import capture_decision
from daimon.core.stores.security_audit import append_event, list_events
from daimon.testing.factories import make_account, make_security_audit_event, make_tenant
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


async def test_migrated_audit_columns_match_orm(db_engine):
    async with db_engine.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'security_audit_events'"
                    )
                )
            ).scalars()
        )
    assert set(SecurityAuditEvent.__table__.columns.keys()) <= columns


async def test_tenant_account_time_filters_and_paging(db_session):
    tenant, other, account = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    t = await make_tenant(db_session, id=tenant)
    await make_tenant(db_session, id=other)
    await make_account(db_session, id=account, tenant=t)
    before = datetime.now(UTC) - timedelta(seconds=1)
    for tid, aid in [(tenant, account), (other, account), (tenant, uuid.uuid4())]:
        await append_event(
            db_session,
            tenant_id=tid,
            account_id=aid,
            agent_id=None,
            platform="discord",
            platform_user_id="123",
            tool_name="read",
            operation=None,
            outcome="allowed",
            reason="completed",
        )
    rows = await list_events(db_session, tenant_id=tenant, since=before)
    assert len(rows) == 2
    assert all(row.tenant_id == tenant for row in rows)
    assert len(await list_events(db_session, tenant_id=tenant, account_id=account)) == 1
    assert (
        await list_events(
            db_session, tenant_id=tenant, since=datetime.now(UTC) + timedelta(seconds=1)
        )
        == []
    )
    assert await list_events(db_session, tenant_id=tenant, offset=1, limit=1) == rows[1:]
    with pytest.raises(ValueError, match="timezone"):
        await list_events(db_session, tenant_id=tenant, since=datetime(2026, 1, 1))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE public.security_audit_events SET reason = 'changed' WHERE false",
        "DELETE FROM public.security_audit_events WHERE false",
        "TRUNCATE public.security_audit_events",
    ],
)
async def test_migration_rejects_all_mutation_even_without_matching_rows(db_engine, statement):
    # Public schema is migrated, whereas per-worker fixture tables use metadata.
    async with db_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="append-only"):
            await connection.execute(text(statement))
        await connection.rollback()


def test_policy_capture_preserves_denial_and_resets_between_requests():
    target = TargetFacts(is_daimon_managed=False, is_reachable_in_tenant=True)
    with capture_decision() as decision:
        assert decide_operation("key_remove", is_admin=False, target=target) == "needs_admin"
        decide_operation("key_add", is_admin=False, target=target)
        assert decision.denied and decision.reason == "needs_admin"
        assert decision.operation == "key_remove"
    with capture_decision() as decision:
        decide_operation("key_add", is_admin=False, target=target)
        assert not decision.denied and decision.reason == "policy_allow"


async def test_maintenance_erases_and_expires_only_scoped_rows_and_resets_guard(db_engine):
    from daimon.core.stores.security_audit import erase_account, prune_events
    from sqlalchemy.ext.asyncio import AsyncSession

    # Exercise the actual migration trigger, not metadata-only fixture tables.
    async with db_engine.connect() as connection, connection.begin():
        await connection.execute(text("SET LOCAL search_path TO public"))
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            tenant = await make_tenant(session)
            other = await make_tenant(session)
            account = await make_account(session, tenant=tenant)
            other_account = await make_account(session, tenant=tenant)
            cutoff = datetime.now(UTC) - timedelta(days=90)
            old = cutoff - timedelta(seconds=1)
            await make_security_audit_event(
                session, tenant_id=tenant.id, account_id=account.id, occurred_at=old
            )
            await make_security_audit_event(
                session, tenant_id=tenant.id, account_id=other_account.id, occurred_at=cutoff
            )
            await make_security_audit_event(
                session, tenant_id=other.id, account_id=account.id, occurred_at=old
            )
            assert await erase_account(session, tenant_id=tenant.id, account_id=account.id) == 1
            rows = await list_events(session, tenant_id=tenant.id)
            assert rows[0].account_id is None and rows[0].platform_user_id is None
            assert rows[1].account_id == other_account.id and rows[1].platform_user_id == "U123"
            assert (await list_events(session, tenant_id=other.id))[0].account_id == account.id
            assert await prune_events(session, tenant_id=tenant.id, older_than=cutoff) == 1
            assert len(await list_events(session, tenant_id=tenant.id)) == 1
            assert len(await list_events(session, tenant_id=other.id)) == 1
            for statement in (
                "UPDATE security_audit_events SET reason = 'oops' WHERE false",
                "DELETE FROM security_audit_events WHERE false",
                "TRUNCATE security_audit_events",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    async with session.begin_nested():
                        await session.execute(text(statement))
        await connection.rollback()


async def test_maintenance_failure_restores_guard(db_engine):
    from daimon.core.stores.security_audit import _maintenance
    from sqlalchemy.ext.asyncio import AsyncSession

    async with db_engine.connect() as connection, connection.begin():
        await connection.execute(text("SET LOCAL search_path TO public"))
        async with AsyncSession(bind=connection) as session:
            with pytest.raises(DBAPIError):
                async with _maintenance(session):
                    await session.execute(text("SELECT 1 / 0"))
            with pytest.raises(DBAPIError, match="append-only"):
                async with session.begin_nested():
                    await session.execute(text("DELETE FROM security_audit_events WHERE false"))
            # TRUNCATE stays forbidden even inside sanctioned maintenance.
            with pytest.raises(DBAPIError, match="append-only"):
                async with _maintenance(session):
                    await session.execute(text("TRUNCATE security_audit_events"))
        await connection.rollback()


async def test_privacy_purge_clears_identifiers_including_unlinked_tenants(
    db_session, db_session_factory
):
    from daimon.core.purge import purge_account

    tenant, other = await make_tenant(db_session), await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    survivor = await make_account(db_session, tenant=tenant)
    # Deliberately no principals: the audit rows must still be found.
    for tid, aid in [(tenant.id, account.id), (other.id, account.id), (tenant.id, survivor.id)]:
        await make_security_audit_event(db_session, tenant_id=tid, account_id=aid)
    await db_session.commit()
    await purge_account(sm=db_session_factory, account_id=account.id)
    async with db_session_factory() as session:
        rows = await list_events(session, tenant_id=tenant.id)
        assert rows[0].account_id is None and rows[0].platform_user_id is None
        assert rows[1].account_id == survivor.id and rows[1].platform_user_id == "U123"
        elsewhere = await list_events(session, tenant_id=other.id)
        assert elsewhere[0].account_id is None and elsewhere[0].platform_user_id is None
        # Simulate an event queued before erasure but written after it.
        late = await append_event(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            agent_id=None,
            platform="slack",
            platform_user_id="U123",
            tool_name="read",
            operation=None,
            outcome="allowed",
            reason="completed",
        )
        assert late is not None and late.account_id is None and late.platform_user_id is None


async def test_tenant_delete_removes_audit_and_late_write_cannot_restore_it(db_session):
    from daimon.core.stores.tenants import delete_tenant

    tenant, other = await make_tenant(db_session), await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_security_audit_event(db_session, tenant_id=tenant.id, account_id=account.id)
    await make_security_audit_event(db_session, tenant_id=other.id, account_id=account.id)
    await delete_tenant(db_session, tenant_id=tenant.id)
    assert await list_events(db_session, tenant_id=tenant.id) == []
    assert len(await list_events(db_session, tenant_id=other.id)) == 1
    late = await append_event(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        agent_id=None,
        platform="slack",
        platform_user_id="U123",
        tool_name="read",
        operation=None,
        outcome="allowed",
        reason="completed",
    )
    assert late is None


@pytest.mark.parametrize("deleted_table", ["accounts", "tenants"])
async def test_old_worker_delete_sql_erases_audit_and_restores_guard(db_engine, deleted_table):
    from sqlalchemy.ext.asyncio import AsyncSession

    # Same terminal DELETE as pre-feature account/tenant stores. No audit-aware
    # application helper is called. Use migrated public tables to test triggers.
    async with db_engine.connect() as connection, connection.begin():
        await connection.execute(text("SET LOCAL search_path TO public"))
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            tenant, other = await make_tenant(session), await make_tenant(session)
            account = await make_account(session, tenant=tenant)
            survivor = await make_account(session, tenant=other)
            for tid, aid in (
                (tenant.id, account.id),
                (other.id, account.id),
                (other.id, survivor.id),
            ):
                await make_security_audit_event(session, tenant_id=tid, account_id=aid)
            target_id = account.id if deleted_table == "accounts" else tenant.id
            async with session.begin_nested() as deletion:
                await session.execute(
                    text(f"DELETE FROM public.{deleted_table} WHERE id = :id"), {"id": target_id}
                )
                here = await list_events(session, tenant_id=tenant.id)
                elsewhere = await list_events(session, tenant_id=other.id)
                if deleted_table == "tenants":
                    assert here == []
                else:
                    assert len(here) == 1
                    assert here[0].account_id is None and here[0].platform_user_id is None
                assert elsewhere[0].account_id is None
                assert elsewhere[0].platform_user_id is None
                assert elsewhere[1].account_id == survivor.id
                assert elsewhere[1].platform_user_id == "U123"
                assert (
                    await session.scalar(
                        text("SELECT current_setting('daimon.security_audit_maintenance', true)")
                    )
                    != "on"
                )
                for statement in (
                    "UPDATE public.security_audit_events SET reason = 'oops' WHERE false",
                    "DELETE FROM public.security_audit_events WHERE false",
                    "TRUNCATE public.security_audit_events",
                ):
                    with pytest.raises(DBAPIError, match="append-only"):
                        async with session.begin_nested():
                            await session.execute(text(statement))
                # An old worker's rollback must restore both identity and audit data.
                await deletion.rollback()
            restored = await list_events(session, tenant_id=tenant.id)
            assert restored[0].account_id == account.id
            assert restored[0].platform_user_id == "U123"
        await connection.rollback()
