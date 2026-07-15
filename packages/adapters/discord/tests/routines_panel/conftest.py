"""Fixtures for routines_panel tests."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core._models import Routine, Tenant
from daimon.core.stores.domain import RoutineRow
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def make_stub_anthropic() -> Callable[
    [Callable[[httpx.Request], httpx.Response] | None], AsyncAnthropic
]:
    return build_stub_anthropic


@pytest.fixture
def stub_anthropic() -> AsyncAnthropic:
    return build_stub_anthropic()


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
        existing = await db_session.get(Tenant, resolved_tenant_id)
        if existing is None:
            ws_id = str(resolved_tenant_id)
            db_session.add(Tenant(id=resolved_tenant_id, platform="discord", external_id=ws_id))
            await db_session.flush()
        orm = Routine(
            tenant_id=resolved_tenant_id,
            created_by_user_id=created_by_user_id,
            agent_id=agent_id,
            agent_name=agent_name,
            cron_expr=cron_expr,
            timezone=timezone,
            trigger_message=trigger_message,
            enabled=enabled,
            next_fire_at=next_fire_at,
            last_fired_at=last_fired_at,
            last_error=last_error,
            last_result_tail=last_result_tail,
        )
        db_session.add(orm)
        await db_session.flush()
        await db_session.refresh(orm)
        return RoutineRow.model_validate(orm)

    return _seed
