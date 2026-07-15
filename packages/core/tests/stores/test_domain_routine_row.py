"""ORM-instance → Pydantic mapping for the Routine row type.

Pure mapping check — no DB session involved. Constructs a `Routine` ORM
instance directly, then validates it through `RoutineRow.model_validate(...,
from_attributes=True)` and asserts each field round-trips.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core._models import Routine
from daimon.core.stores.domain import RoutineRow


def test_routine_row_validates_from_orm_instance_with_all_fields_populated() -> None:
    routine_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    created = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    updated = datetime(2026, 5, 8, 12, 5, tzinfo=UTC)
    fired = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    next_fire = datetime(2026, 5, 9, 9, 0, tzinfo=UTC)

    orm = Routine(
        id=routine_id,
        tenant_id=tenant_id,
        created_by_user_id="user-abc",
        agent_id="agt_123",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone="UTC",
        trigger_message="run morning standup",
        enabled=True,
        next_fire_at=next_fire,
        last_fired_at=fired,
        last_error=None,
        last_result_tail="ok",
        created_at=created,
        updated_at=updated,
    )

    row = RoutineRow.model_validate(orm, from_attributes=True)

    assert row.id == routine_id, "id should round-trip"
    assert row.tenant_id == tenant_id, "tenant_id should round-trip"
    assert row.created_by_user_id == "user-abc", "created_by_user_id should round-trip"
    assert row.agent_id == "agt_123", "agent_id should round-trip"
    assert row.cron_expr == "0 9 * * *", "cron_expr should round-trip"
    assert row.timezone == "UTC", "timezone should round-trip"
    assert row.trigger_message == "run morning standup", "trigger_message should round-trip"
    assert row.enabled is True, "enabled should round-trip as True"
    assert row.next_fire_at == next_fire, "next_fire_at should round-trip"
    assert row.last_fired_at == fired, "last_fired_at should round-trip"
    assert row.last_error is None, "last_error should round-trip None"
    assert row.last_result_tail == "ok", "last_result_tail should round-trip"
    assert row.created_at == created, "created_at should round-trip"
    assert row.updated_at == updated, "updated_at should round-trip"


def test_routine_row_handles_optional_fields_as_none() -> None:
    orm = Routine(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=None,
        agent_id="agt_1",
        agent_name="daimon",
        cron_expr="*/5 * * * *",
        timezone="America/New_York",
        trigger_message="ping",
        enabled=True,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
        updated_at=datetime(2026, 5, 8, tzinfo=UTC),
    )

    row = RoutineRow.model_validate(orm, from_attributes=True)

    assert row.created_by_user_id is None, "created_by_user_id can be None"
    assert row.next_fire_at is None, "next_fire_at can be None before first scheduling"
    assert row.last_fired_at is None, "last_fired_at None until first fire"
    assert row.last_error is None, "last_error None when never errored"
    assert row.last_result_tail is None, "last_result_tail None until first result"
    assert row.enabled is True, "enabled round-trips True"
