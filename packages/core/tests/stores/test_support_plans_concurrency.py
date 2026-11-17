"""Support plans under concurrent submits and settles: real Postgres, separate connections."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as plans
from daimon.core.support_plans import RequestChoice, SupportPlace, parse_catalog
from daimon.core.windows import WindowBounds
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

D = Decimal
PLACE = SupportPlace(channel_id="C1", agent_id=None)
MENU = parse_catalog(
    {
        "name": "Support",
        "options": [
            {"key": "quick", "label": "Quick", "price": {"kind": "fixed", "amount": "0.5"}},
            {
                "key": "review",
                "label": "Review",
                "price": {"kind": "range", "min": "1", "max": "3"},
            },
        ],
    }
)

type Factory = async_sessionmaker[AsyncSession]


async def _setup(session: AsyncSession, *, pool: Decimal) -> tuple[uuid.UUID, int]:
    tenant = await make_tenant(session, platform="slack", workspace_id="T_PLAN_RACE")
    menu = await plans.set_catalog(
        session, tenant_id=tenant.id, key="support", spec=MENU, set_by_account_id=None
    )
    await plans.set_plan(
        session,
        tenant_id=tenant.id,
        platform="slack",
        scope="workspace",
        scope_id="",
        catalog_id=menu.id,
        pool=pool,
        per_person=None,
        window=WindowBounds("total", None, None),
        set_by_account_id=None,
    )
    await session.commit()
    return tenant.id, menu.revision


async def _submit(
    factory: Factory, tenant_id: uuid.UUID, choice: RequestChoice, *, user: str, message: str
) -> ledger.SubmitOutcome:
    async with factory() as session, session.begin():
        outcome = await ledger.submit_request(
            session,
            tenant_id=tenant_id,
            account_id=None,
            platform="slack",
            platform_user_id=user,
            channel_id="C1",
            message_id=message,
            ma_session_id=None,
            place=PLACE,
            choice=choice,
            default_allowance=20,
        )
        await asyncio.sleep(0.1)  # hold the locks so the others really overlap
        return outcome


async def _settle(factory: Factory, escalation_id: uuid.UUID, charge: Decimal) -> str:
    async with factory() as session, session.begin():
        outcome = await ledger.settle_request(
            session,
            escalation_id=escalation_id,
            charge=charge,
            settled_by="staff",
            settled_by_account_id=None,
            default_allowance=20,
        )
        await asyncio.sleep(0.1)
        return outcome.status


async def test_submit_when_eight_people_race_for_a_pool_of_two_then_four_are_recorded(
    db_session: AsyncSession, db_committing_engine: AsyncEngine
) -> None:
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant_id, revision = await _setup(db_session, pool=D(2))
    quick = RequestChoice("quick", revision)
    results = await asyncio.gather(
        *(_submit(factory, tenant_id, quick, user=f"U{i}", message=f"m{i}") for i in range(8))
    )
    statuses = sorted(r.status for r in results)
    assert statuses == ["out_of_credits"] * 4 + ["recorded"] * 4, (
        "the plan lock serializes people on one pool: 8 x 0.5 into 2 records 4"
    )


async def test_settle_when_two_staff_click_at_once_then_one_wins(
    db_session: AsyncSession, db_committing_engine: AsyncEngine
) -> None:
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant_id, revision = await _setup(db_session, pool=D(5))
    outcome = await _submit(
        factory, tenant_id, RequestChoice("review", revision), user="U1", message="m1"
    )
    assert outcome.row, "recorded"
    results = await asyncio.gather(
        _settle(factory, outcome.row.id, D(2)), _settle(factory, outcome.row.id, D(3))
    )
    assert sorted(results) == ["already_settled", "settled"], "one settle, one sees it done"


async def test_submit_and_settle_when_concurrent_then_both_finish_without_deadlock(
    db_session: AsyncSession, db_committing_engine: AsyncEngine
) -> None:
    factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    tenant_id, revision = await _setup(db_session, pool=D(3))
    review = RequestChoice("review", revision)
    first = await _submit(factory, tenant_id, review, user="U1", message="m1")
    assert first.row, "recorded"
    submit, settle = await asyncio.wait_for(
        asyncio.gather(
            _submit(factory, tenant_id, review, user="U1", message="m2"),
            _settle(factory, first.row.id, D(2)),
        ),
        timeout=10,
    )
    assert settle == "settled", "the settle went through"
    assert submit.status in ("recorded", "out_of_credits"), "the submit finished either way"
    async with factory() as session:
        plan = await plans.get_plan(session, tenant_id=tenant_id, scope="workspace", scope_id="")
        assert plan, "the plan is there"
        spent, _ = await ledger.plan_spent(
            session, plan=plan, platform_user_id="U1", as_of=first.row.created_at
        )
    assert spent <= D(3), "the pool is never overspent whichever ran first"
