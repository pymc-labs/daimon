"""Stale resume controls give an ordinary reply without retiming a running routine."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from daimon.core.errors import RoutineAlreadyRunning, StoreError
from daimon.core.posted_controls.routines import apply_routine_action
from daimon.core.stores.domain import RoutineRow


@pytest.mark.parametrize(
    "enabled, error", [(True, None), (False, RoutineAlreadyRunning("This routine is already on."))]
)
async def test_resume_already_running_is_a_normal_result(enabled, error):
    now = datetime.now(UTC)
    row = RoutineRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id="owner",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone="UTC",
        trigger_message="run",
        enabled=enabled,
        next_fire_at=now,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        created_at=now,
        updated_at=now,
    )
    resume = AsyncMock(side_effect=error)
    pause = AsyncMock()
    result = await apply_routine_action(
        AsyncMock(),
        row.id,
        tenant_id=row.tenant_id,
        op="resume",
        allowed=AsyncMock(return_value=True),
        load=AsyncMock(return_value=row),
        pause=pause,
        resume=resume,
        now=lambda: now,
    )
    assert result == "already_running"
    pause.assert_not_awaited()
    if enabled:
        resume.assert_not_awaited()
    else:
        resume.assert_awaited_once()


async def test_unrelated_resume_failure_is_not_reported_as_already_running():
    now = datetime.now(UTC)
    row = RoutineRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id="owner",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone="UTC",
        trigger_message="run",
        enabled=False,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        created_at=now,
        updated_at=now,
    )
    with pytest.raises(StoreError, match="changed"):
        await apply_routine_action(
            AsyncMock(),
            row.id,
            tenant_id=row.tenant_id,
            op="resume",
            allowed=AsyncMock(return_value=True),
            load=AsyncMock(return_value=row),
            pause=AsyncMock(),
            resume=AsyncMock(side_effect=StoreError("changed")),
            now=lambda: now,
        )
