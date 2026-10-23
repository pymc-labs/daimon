"""Routine behavior edits adopt their authenticated author atomically."""

from datetime import UTC, datetime

import pytest
from daimon.core.errors import StoreError
from daimon.core.stores import routines
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.parametrize(
    "change",
    [
        {"trigger_message": "new task"},
        {"agent_id": "new-agent"},
        {"agent_name": "new-name"},
        {"destination_kind": "channel", "destination_id": "other", "channel_id": "other"},
        {"clear_destination": True},
    ],
)
async def test_behavior_edit_transfers_authority_and_audits(
    db_session: AsyncSession, change: dict[str, object]
) -> None:
    tenant = await make_tenant(db_session)
    row = await routines.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="owner",
        agent_id="agent",
        agent_name="Helper",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="old task",
        destination_kind="channel",
        destination_id="channel",
        channel_id="channel",
    )
    updated = await routines.update_routine(
        db_session,
        row.id,
        tenant_id=tenant.id,
        edited_by="delegate",
        expected_updated_at=row.updated_at,
        **change,
    )
    assert updated is not None and updated.created_by_user_id == "delegate"
    audit = await list_events(db_session, tenant_id=tenant.id, tool_name="routines")
    assert len(audit) == 1
    assert audit[0].operation == "transfer_authority"
    assert audit[0].platform_user_id == "delegate"
    assert audit[0].admin_subject_id == "owner"
    assert audit[0].turn_ref == f"routine:{row.id}"
    assert audit[0].occurred_at.tzinfo is not None
    with pytest.raises(StoreError, match="changed while"):
        await routines.update_routine(
            db_session,
            row.id,
            tenant_id=tenant.id,
            edited_by="owner",
            expected_updated_at=row.updated_at,
            trigger_message="stale attempt",
        )
    assert len(await list_events(db_session, tenant_id=tenant.id, tool_name="routines")) == 1


async def test_missing_editor_fails_closed_and_pause_resume_never_transfer(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    row = await routines.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="owner",
        agent_id="agent",
        agent_name="Helper",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="old task",
    )
    with pytest.raises(StoreError, match="platform user id"):
        await routines.update_routine(
            db_session, row.id, tenant_id=tenant.id, trigger_message="bad"
        )
    paused = await routines.pause_routine(db_session, row.id, tenant_id=tenant.id)
    assert paused is not None and paused.created_by_user_id == "owner"
    resumed = await routines.resume_routine(
        db_session, row.id, tenant_id=tenant.id, now=datetime.now(UTC)
    )
    assert resumed is not None and resumed.created_by_user_id == "owner"
    assert not await list_events(db_session, tenant_id=tenant.id, tool_name="routines")


async def test_private_transfer_redirects_to_editor_and_erases_prior_output(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    row = await routines.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="111111111111111111",
        agent_id="agent",
        agent_name="Helper",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="old task",
        destination_kind="dm",
        destination_id="111111111111111111",
        source_provenance_known=True,
    )
    await routines.record_result(
        db_session,
        row.id,
        tail="private result",
        error="private error",
        delivery="pending",
        delivery_payload="private delivery",
    )
    before = await routines.get_routine(db_session, row.id, tenant_id=tenant.id)
    assert before is not None and before.last_result_tail == "private result"
    changed = await routines.update_routine(
        db_session,
        row.id,
        tenant_id=tenant.id,
        edited_by="222222222222222222",
        trigger_message="new task",
    )
    assert changed is not None
    assert changed.created_by_user_id == changed.destination_id == "222222222222222222"
    assert changed.last_result_tail is None and changed.delivery_payload is None
    assert changed.delivery_lease_owner is None
