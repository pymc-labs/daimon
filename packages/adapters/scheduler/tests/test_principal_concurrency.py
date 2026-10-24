"""Concurrent scheduled turns reuse an owner even while enrollment is committing."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.scheduler.main import _build_fire  # pyright: ignore[reportPrivateUsage]
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scheduler import RoutineDispatcher, run_one_tick
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.routines import create_routine, get_routine
from daimon.testing.factories import make_ledger_entry, make_platform_principal, make_tenant
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .test_main import _make_test_settings  # pyright: ignore[reportPrivateUsage]


class NoCaps:
    async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool:
        return False


@pytest.mark.parametrize("enrollment_committed", [True, False], ids=["committed", "committing"])
async def test_concurrent_routines_reuse_existing_owner(
    db_session: AsyncSession,
    db_committing_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    enrollment_committed: bool,
) -> None:
    """Both lookups are real SELECTs on separate Postgres connections.

    In the committing case, the principal already exists in a third transaction,
    but neither lookup can see it. Commit enrollment only after both SELECTs:
    each old SELECT-then-INSERT then fails with uq_platform_principal.
    """
    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    now = datetime.now(UTC)
    owner = "474903109790072844"
    plain = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=owner,
        agent_id="agent_principal",
        agent_name="daimon",
        cron_expr="0 0 * * *",
        timezone_="UTC",
        trigger_message="plain",
        next_fire_at=now - timedelta(seconds=1),
    )
    once = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=owner,
        agent_id="agent_principal",
        agent_name="daimon",
        cron_expr="0 0 * * *",
        timezone_="UTC",
        trigger_message="once",
        next_fire_at=now - timedelta(days=2),
        catch_up_policy="run-once",
    )
    await db_session.commit()
    lookups = 0
    both_looked_up = asyncio.Event()

    def after_select(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal lookups
        if statement.startswith("SELECT ") and "platform_principals" in statement:
            lookups += 1
            if lookups == 2:
                both_looked_up.set()

    accounts: list[object] = []
    turns_started = asyncio.Barrier(2)

    async def run_turn(**kwargs: object) -> str:
        accounts.append(kwargs["account_id"])
        await turns_started.wait()
        return "finished"

    async def resolve(*args: object, **kwargs: object) -> str:
        return "agent_principal"

    dispatcher = RoutineDispatcher(2, clock=lambda: now)
    async with (
        AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999") as client,
        sm() as enrollment,
    ):
        principal = await make_platform_principal(
            enrollment, tenant=tenant, platform="discord", external_id=owner
        )
        if enrollment_committed:
            await enrollment.commit()
        fire = await _build_fire(
            client=client,
            sm=sm,
            settings=_make_test_settings(monkeypatch),
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
        )
        event.listen(db_committing_engine.sync_engine, "after_cursor_execute", after_select)
        try:
            with (
                patch("daimon.adapters.scheduler.main.run_turn", side_effect=run_turn),
                patch("daimon.adapters.scheduler.main.resolve_agent", side_effect=resolve),
                patch("daimon.adapters.scheduler.main.resolve_environment", side_effect=resolve),
            ):
                await run_one_tick(
                    now=now,
                    sm=sm,
                    caps=NoCaps(),
                    fire=fire,
                    max_age=timedelta(minutes=15),
                    max_concurrent_fires=2,
                    dispatch_timeout_s=20,
                    dispatcher=dispatcher,
                )
                await asyncio.wait_for(both_looked_up.wait(), timeout=10)
                await enrollment.commit()
                await asyncio.wait_for(dispatcher.drain(), timeout=10)
        finally:
            await dispatcher.close()
            event.remove(db_committing_engine.sync_engine, "after_cursor_execute", after_select)

    async with sm() as session:
        saved = [await get_routine(session, row.id, tenant_id=tenant.id) for row in (plain, once)]
        assert all(row is not None and row.last_error is None for row in saved), saved
        assert all(row is not None and row.last_result_tail == "finished" for row in saved)
        assert all(row is not None and row.last_fired_at == now for row in saved)
        existing = await find_platform_principal(
            session, tenant_id=tenant.id, platform="discord", external_id=owner
        )
        assert existing == principal
        assert accounts == [principal.account_id, principal.account_id]
        # Losing enrollment attempts must not leave orphan accounts behind.
        assert (
            await session.scalar(
                text("SELECT count(*) FROM accounts WHERE tenant_id = :tenant"),
                {"tenant": tenant.id},
            )
            == 1
        )
