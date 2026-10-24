"""Fixtures for routines_panel tests."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

import pytest
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import (
    create_routine,
    get_routine,
    record_result,
    set_last_fired_at,
)
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import make_stub_anthropic, stub_anthropic  # noqa: F401
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def account_id() -> uuid.UUID:
    return uuid.uuid4()


SeedRoutineFn = Callable[..., Awaitable[RoutineRow]]


@pytest.fixture
def seed_routine(db_session: AsyncSession) -> SeedRoutineFn:
    """Async factory: insert a Routine row and return its RoutineRow.

    `routines.tenant_id` is NOT NULL with an FK to `tenants.id` (migration
    0014), so each seed mints a Tenant row to satisfy the FK and stamps its id
    onto the routine. Callers may pass an explicit `tenant_id`; when supplied,
    the FK target is created (idempotently) under that id.
    """

    async def _seed(
        *,
        tenant_id: uuid.UUID | None = None,
        agent_id: str = "agent_a",
        agent_name: str = "daimon",
        enabled: bool = True,
        last_fired_at: datetime | None = None,
        last_error: str | None = None,
        last_result_tail: str | None = None,
        next_fire_at: datetime | None = None,
        created_by_user_id: str | None = None,
        cron_expr: str = "0 9 * * 1-5",
        timezone: str = "UTC",
        trigger_message: str = "summarize yesterday's commits",
    ) -> RoutineRow:
        resolved_tenant_id = tenant_id if tenant_id is not None else uuid.uuid4()
        if await get_tenant(db_session, resolved_tenant_id) is None:
            ws_id = str(resolved_tenant_id)
            await make_tenant(
                db_session, platform="discord", workspace_id=ws_id, id=resolved_tenant_id
            )
        row = await create_routine(
            db_session,
            tenant_id=resolved_tenant_id,
            created_by_user_id=created_by_user_id,
            agent_id=agent_id,
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone_=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
            next_fire_at=next_fire_at,
        )
        if last_fired_at is not None:
            await set_last_fired_at(db_session, row.id, last_fired_at=last_fired_at)
        if last_error is not None or last_result_tail is not None:
            await record_result(db_session, row.id, tail=last_result_tail, error=last_error)
        if last_fired_at is not None or last_error is not None or last_result_tail is not None:
            refreshed = await get_routine(db_session, row.id, tenant_id=resolved_tenant_id)
            assert refreshed is not None, "seeded routine must still resolve after the update"
            return refreshed
        return row

    return _seed
