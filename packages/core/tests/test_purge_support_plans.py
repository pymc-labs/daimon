"""Erasure on support plans: the person leaves their plan rows, the charges stay (decision 7)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from daimon.core._models import SupportAllowance, SupportEscalation
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account, purge_principal
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as plans
from daimon.core.support_plans import RequestChoice, SupportPlace, parse_catalog
from daimon.core.windows import WindowBounds
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
GONE, STAY = "U_GONE", "U_STAY"
MENU = parse_catalog(
    {
        "name": "Support",
        "options": [
            {
                "key": "review",
                "label": "Review",
                "price": {"kind": "range", "min": "1", "max": "3"},
                "fields": [{"key": "note", "kind": "paragraph", "label": "What do you need?"}],
            }
        ],
    }
)

type Factory = async_sessionmaker[AsyncSession]


async def _person_with_plan_history(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """GONE asked on the plan and on the default, holds an allowance and settled STAY's request."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PURGE_PLAN")
    account = await make_account(db_session, tenant=tenant)
    principal = await make_platform_principal(
        db_session, platform="slack", external_id=GONE, tenant=tenant, account=account
    )
    menu = await plans.set_catalog(
        db_session, tenant_id=tenant.id, key="support", spec=MENU, set_by_account_id=None
    )
    plan = await plans.set_plan(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        scope="channel",
        scope_id="C1",
        catalog_id=menu.id,
        pool=D(10),
        per_person=None,
        window=WindowBounds("total", None, None),
        set_by_account_id=None,
    )
    await plans.set_allowance(
        db_session,
        tenant_id=tenant.id,
        plan_id=plan.id,
        platform_user_id=GONE,
        credits=D(4),
        set_by_account_id=None,
    )
    choice = RequestChoice("review", menu.revision, answers={"note": "my private words"})
    ids: dict[str, uuid.UUID] = {}
    for user, account_id in ((GONE, account.id), (STAY, None)):
        outcome = await ledger.submit_request(
            db_session,
            tenant_id=tenant.id,
            account_id=account_id,
            platform="slack",
            platform_user_id=user,
            channel_id="C1",
            message_id=f"m-{user}",
            ma_session_id=f"sesn-{user}",
            place=SupportPlace(channel_id="C1", agent_id=None),
            choice=choice,
            default_allowance=20,
        )
        assert outcome.row, f"{user} recorded on the plan"
        ids[user] = outcome.row.id
    settled = await ledger.settle_request(
        db_session,
        escalation_id=ids[STAY],
        charge=D("1.5"),
        settled_by=GONE,
        settled_by_account_id=account.id,
        default_allowance=20,
    )
    assert settled.status == "settled", "GONE settled STAY's request as staff"
    legacy = await ledger.record_escalation(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        platform_user_id=GONE,
        channel_id="C2",
        message_id="m-legacy",
        ma_session_id=None,
        note="old ask",
        allowance=20,
    )
    assert legacy, "a default-ledger row"
    await db_session.commit()
    return principal.id, account.id


async def _rows(db_session: AsyncSession) -> list[SupportEscalation]:
    db_session.expire_all()
    stmt = select(SupportEscalation).order_by(SupportEscalation.message_id)
    return list((await db_session.execute(stmt)).scalars().all())


async def test_purge_principal_when_plan_history_then_anonymizes_and_keeps_charges(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    principal_id, _ = await _person_with_plan_history(db_session)
    report = (
        await purge_principal(sm=db_session_factory, principal_id=principal_id, kind="platform")
    ).db
    counts = (
        report.support_escalations,
        report.support_requests_anonymized,
        report.support_allowances,
        report.support_settlers_cleared,
    )
    assert counts == (1, 1, 1, 1), "legacy row deleted; plan row, allowance and settler handled"
    rows = {r.message_id: r for r in await _rows(db_session)}
    assert set(rows) == {f"m-{GONE}", f"m-{STAY}"}, "only the default-ledger row is deleted"
    gone = rows[f"m-{GONE}"]
    assert (gone.platform_user_id, gone.account_id, gone.ma_session_id) == (
        ledger.PURGED_USER,
        None,
        None,
    ), "the person is erased from their plan row"
    assert (gone.note, gone.answers) == ("", None), "their words go with them"
    assert (gone.status, gone.hold) == ("pending", D(1)), "the pool keeps the hold"
    stay = rows[f"m-{STAY}"]
    assert (stay.settled_by, stay.settled_by_account_id) == (None, None), "the settler is cleared"
    assert (stay.platform_user_id, stay.charge) == (STAY, D("1.5")), "the bystander's row stays"
    allowances = (await db_session.execute(select(SupportAllowance))).scalars().all()
    assert list(allowances) == [], "the person's allowance is deleted"
    again = (
        await purge_principal(sm=db_session_factory, principal_id=principal_id, kind="platform")
    ).db
    assert again.support_requests_anonymized == 0, "a re-run finds no one left to erase"


async def test_purge_account_when_plan_history_then_matches_its_preview(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    _, account_id = await _person_with_plan_history(db_session)
    preview = await collect_purge_preview(sm=db_session_factory, account_id=account_id)
    report = (await purge_account(sm=db_session_factory, account_id=account_id)).db
    assert (
        (
            preview.support_requests_anonymized.count,
            preview.support_allowances.count,
            preview.support_settlers_cleared.count,
        )
        == (
            report.support_requests_anonymized,
            report.support_allowances,
            report.support_settlers_cleared,
        )
        == (1, 1, 1)
    ), "the preview names exactly what the purge does"
    rows = await _rows(db_session)
    assert [r.platform_user_id for r in rows] == [ledger.PURGED_USER, STAY], "plan rows remain"
