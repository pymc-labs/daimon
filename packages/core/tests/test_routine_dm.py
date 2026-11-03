"""Owner-only destinations keep user IDs out of channel resolution."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.errors import StoreError
from daimon.core.routine_delivery import delivery_target, destination_shape_error
from daimon.core.routines import has_private_output
from daimon.core.rule_views import routine_destination_channel
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.routines import create_routine, record_result, update_routine
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


async def _create(session: AsyncSession, **overrides):
    tenant = await make_tenant(session, platform=overrides.pop("platform", "discord"))
    values = dict(
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="summarize",
        destination_kind="dm",
    )
    values.update(overrides)
    return await create_routine(session, **values)


async def test_dm_resolves_owner_and_retains_source_budget(db_session: AsyncSession) -> None:
    row = await _create(db_session, channel_id="456")
    assert row.destination_id == row.created_by_user_id == "123"
    assert has_private_output(row)
    assert routine_destination_channel(row) is None
    assert delivery_target(row, platform="discord") is None
    await record_result(db_session, row.id, tail="private", error=None, delivery="pending")
    updated = await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=row.tenant_id,
        destination_kind="channel",
        destination_id="456",
        channel_id="456",
    )
    assert updated is not None
    assert updated.delivery_status == "skipped"
    assert updated.delivery_payload is None
    assert not has_private_output(updated)


@pytest.mark.parametrize(
    "overrides",
    [
        {"created_by_user_id": None},
        {"destination_id": "999"},
        {"platform": "slack"},
        {"platform": "teams"},
    ],
)
async def test_dm_refuses_other_recipient_unowned_and_unsupported(db_session, overrides) -> None:
    with pytest.raises(StoreError):
        await _create(db_session, **overrides)


async def test_dm_update_preserves_origin_and_refuses_new_source_seal(db_session) -> None:
    row = await _create(
        db_session, destination_kind="channel", destination_id="456", channel_id="456"
    )
    updated = await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=row.tenant_id,
        destination_kind="dm",
    )
    assert updated is not None and updated.channel_id == "456" and updated.destination_id == "123"
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(channel_rules={"456": ChannelRule(readers="inside")}),
    )
    with pytest.raises(StoreError, match="restricted source"):
        await update_routine(
            db_session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )


@pytest.mark.parametrize("platform", ["slack", "teams"])
def test_unsupported_platform_has_explicit_dm_error(platform: str) -> None:
    assert (
        destination_shape_error(platform, "dm", "123")
        == "DM routines are supported only on Discord"
    )


@pytest.mark.fresh_schema
async def test_dm_migration_upgrade_constraints_and_guarded_downgrade(db_session) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0135_routine_dm_destination.py"
    spec = importlib.util.spec_from_file_location("migration_dm", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    conn = await db_session.connection()

    def migrate(sync_conn, direction):
        with Operations.context(MigrationContext.configure(sync_conn)):
            getattr(migration, direction)()

    await conn.run_sync(migrate, "downgrade")
    await conn.run_sync(migrate, "upgrade")
    row = await _create(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE routines SET destination_id = '999'"))
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(text("UPDATE routines SET created_by_user_id = NULL"))
    with pytest.raises(RuntimeError, match="convert or remove"):
        await conn.run_sync(migrate, "downgrade")
    await db_session.execute(text("DELETE FROM routines WHERE id = :id"), {"id": row.id})
    await conn.run_sync(migrate, "downgrade")
    await conn.run_sync(migrate, "upgrade")


async def test_result_computed_for_dm_cannot_be_queued_after_public_redirect(db_session):
    row = await _create(db_session)
    await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=row.tenant_id,
        destination_kind="channel",
        destination_id="456",
    )
    await record_result(
        db_session,
        row.id,
        tail="personal memory",
        error=None,
        delivery="pending",
        expected_destination=("dm", "123"),
    )
    from daimon.core.stores.routines import get_routine

    after = await get_routine(db_session, row.id, tenant_id=row.tenant_id)
    assert after is not None and after.delivery_payload is None and after.last_result_tail is None


async def test_unknown_thread_never_promotes_from_a_new_budget_channel(db_session):
    row = await _create(db_session, destination_kind="thread", destination_id="789")
    for budget in (None, "456", "999"):
        with pytest.raises(StoreError, match="Recreate the routine"):
            await update_routine(
                db_session,
                row.id,
                edited_by=row.created_by_user_id,
                tenant_id=row.tenant_id,
                destination_kind="dm",
                channel_id=budget,
            )


async def test_thread_conversion_preserves_source_and_checks_later_seals(db_session):
    from daimon.core.rule_views import keeps_routine_inside

    row = await _create(
        db_session, destination_kind="thread", destination_id="789", channel_id="456"
    )
    policy = TenantAccessPolicy(channel_rules={"789": ChannelRule(readers="inside")})
    await set_access_policy(db_session, tenant_id=row.tenant_id, policy=policy)
    with pytest.raises(StoreError, match="restricted source"):
        await update_routine(
            db_session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )
    await set_access_policy(db_session, tenant_id=row.tenant_id, policy=TenantAccessPolicy())
    converted = await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=row.tenant_id,
        destination_kind="dm",
    )
    assert converted is not None
    assert (converted.channel_id, converted.source_thread_id) == ("456", "789")
    await set_access_policy(db_session, tenant_id=row.tenant_id, policy=policy)
    assert keeps_routine_inside(policy, converted), "the fire-time gate retains thread provenance"
    with pytest.raises(StoreError, match="restricted source"):
        await update_routine(
            db_session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            trigger_message="admin edit",
        )


async def test_unknown_legacy_source_cannot_be_converted_to_dm(db_session):
    from sqlalchemy import text

    row = await _create(db_session, destination_kind=None)
    await db_session.execute(
        text("UPDATE routines SET source_provenance_known = false WHERE id = :id"), {"id": row.id}
    )
    with pytest.raises(StoreError, match="Recreate the routine"):
        await update_routine(
            db_session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )


async def test_unknown_thread_clears_without_changing_creation_provenance(db_session):
    row = await _create(db_session, destination_kind="thread", destination_id="789")
    cleared = await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=row.tenant_id,
        clear_destination=True,
        channel_id="456",
    )
    assert cleared is not None
    assert (cleared.source_channel_id, cleared.source_thread_id) == (None, "789")
    assert not cleared.source_provenance_known
    with pytest.raises(StoreError, match="Recreate the routine"):
        await update_routine(
            db_session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )


async def test_source_pair_survives_changing_budget_and_clearing_delivery(db_session):
    row = await _create(
        db_session, destination_kind="thread", destination_id="789", channel_id="456"
    )
    moved = await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=row.tenant_id,
        destination_kind="channel",
        destination_id="999",
        channel_id="999",
    )
    assert moved is not None and moved.channel_id == "999"
    assert (moved.source_channel_id, moved.source_thread_id) == ("456", "789")
    cleared = await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=row.tenant_id,
        clear_destination=True,
    )
    assert cleared is not None and cleared.channel_id == "999"
    assert (cleared.source_channel_id, cleared.source_thread_id) == ("456", "789")
    await set_access_policy(
        db_session,
        tenant_id=row.tenant_id,
        policy=TenantAccessPolicy(channel_rules={"789": ChannelRule(readers="inside")}),
    )
    with pytest.raises(StoreError, match="restricted source"):
        await update_routine(
            db_session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize("initial_kind", [None, "channel", "thread"])
async def test_legacy_public_edits_never_promote_creation_provenance(
    db_session, platform, initial_kind
):
    from daimon.core.rule_views import keeps_routine_inside

    row = await _create(
        db_session,
        platform=platform,
        destination_kind=initial_kind,
        destination_id="789" if initial_kind else None,
        channel_id="456",
        source_provenance_known=False,
    )
    original = (row.source_channel_id, row.source_thread_id, row.source_provenance_known)
    for kind, target, parent in [
        ("channel", "999", "999"),
        ("thread", "888", "999"),
        (None, None, None),
    ]:
        edited = await update_routine(
            db_session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind=kind,
            destination_id=target,
            channel_id=parent,
            clear_destination=kind is None,
        )
        assert edited is not None
        assert (
            edited.source_channel_id,
            edited.source_thread_id,
            edited.source_provenance_known,
        ) == original
        assert not keeps_routine_inside(TenantAccessPolicy(), edited), (
            "legacy public runs remain available"
        )
    with pytest.raises(StoreError):
        await update_routine(
            db_session,
            row.id,
            edited_by=row.created_by_user_id,
            tenant_id=row.tenant_id,
            destination_kind="dm",
        )
