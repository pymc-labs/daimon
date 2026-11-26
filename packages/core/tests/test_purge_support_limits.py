"""Erasure on support limits: the person leaves their limit rows, the charges stay."""

from __future__ import annotations

import uuid
from decimal import Decimal

from daimon.core._models import SupportEscalation, SupportLimit
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account, purge_principal
from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.support import RequestChoice, SupportPlace, parse_menu
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_support_escalation,
    make_tenant,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
GONE, STAY = "U_GONE", "U_STAY"
MENU = parse_menu(
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


async def _person_with_limit_history(db_session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """GONE asked on C1's limit and on the default, has two own limits, settled STAY's request."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PURGE_PLAN")
    account = await make_account(db_session, tenant=tenant)
    principal = await make_platform_principal(
        db_session, platform="slack", external_id=GONE, tenant=tenant, account=account
    )
    menu = await store.set_menu(
        db_session, tenant_id=tenant.id, key="support", spec=MENU, set_by_account_id=None
    )
    await store.set_menu_use(
        db_session,
        tenant_id=tenant.id,
        scope="workspace",
        scope_id="",
        menu_id=menu.id,
        set_by_account_id=None,
    )
    places = (
        ("channel", "C1", None, D(10)),
        ("channel", "C1", GONE, None),
        ("workspace", "", GONE, None),
    )
    for scope, scope_id, person, shared in places:
        await store.set_limit(
            db_session,
            tenant_id=tenant.id,
            scope=scope,
            scope_id=scope_id,
            platform_user_id=person,
            shared=shared,
            each_person=None if shared else D(4),
            window="total",
            starts_at=None,
            ends_at=None,
            set_by_account_id=None,
        )
    choice = RequestChoice.of(MENU.options[0], answers={"note": "my private words"})
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
        assert outcome.row, f"{user} recorded on the limit"
        ids[user] = outcome.row.id
    await ledger.mark_delivered(
        db_session,
        escalation_id=ids[GONE],
        support_channel_id="S1",
        support_post_id="post-1",
    )
    settled = await ledger.settle_request(
        db_session,
        escalation_id=ids[STAY],
        charge=D("1.5"),
        settled_by=GONE,
        settled_by_account_id=account.id,
        default_allowance=20,
    )
    assert settled.status == "settled", "GONE settled STAY's request as staff"
    await make_support_escalation(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform_user_id=GONE,
        channel_id="C2",
        message_id="m-legacy",
        note="old ask",
    )  # a default-ledger row
    await db_session.commit()
    return principal.id, account.id


async def _rows(db_session: AsyncSession) -> list[SupportEscalation]:
    db_session.expire_all()
    stmt = select(SupportEscalation).order_by(SupportEscalation.message_id)
    return list((await db_session.execute(stmt)).scalars().all())


async def test_purge_principal_when_limit_history_then_anonymizes_and_keeps_charges(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    principal_id, _ = await _person_with_limit_history(db_session)
    report = (
        await purge_principal(sm=db_session_factory, principal_id=principal_id, kind="platform")
    ).db
    counts = (
        report.support_escalations,
        report.support_requests_anonymized,
        report.support_person_limits,
        report.support_settlers_cleared,
    )
    assert counts == (1, 1, 2, 1), "legacy row deleted; limit row, own limits and settler handled"
    rows = {r.platform_user_id: r for r in await _rows(db_session)}
    assert set(rows) == {ledger.PURGED_USER, STAY}, "only the default-ledger row is deleted"
    gone = rows[ledger.PURGED_USER]
    assert (gone.account_id, gone.ma_session_id, gone.message_id) == (
        None,
        None,
        "",
    ), "the person and their origin message are erased from their limit row"
    assert gone.support_post_id == "post-1", "the support post stays, a settle needs it"
    assert (gone.note, gone.answers) == ("", None), "their words go with them"
    assert (gone.status, gone.hold) == ("pending", D(1)), "the shared credits keep the hold"
    assert (gone.limit_id is not None, gone.person_limit_id) == (True, None), "own limit unlinked"
    stay = rows[STAY]
    assert (stay.settled_by, stay.settled_by_account_id) == (None, None), "the settler is cleared"
    assert (stay.platform_user_id, stay.charge) == (STAY, D("1.5")), "the bystander's row stays"
    limits = (await db_session.execute(select(SupportLimit))).scalars().all()
    assert [lim.platform_user_id for lim in limits] == [None], "only C1's own limit is left"
    again = (
        await purge_principal(sm=db_session_factory, principal_id=principal_id, kind="platform")
    ).db
    assert again.support_requests_anonymized == 0, "a re-run finds no one left to erase"


async def test_purge_account_when_limit_history_then_matches_its_preview(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    _, account_id = await _person_with_limit_history(db_session)
    preview = await collect_purge_preview(sm=db_session_factory, account_id=account_id)
    report = (await purge_account(sm=db_session_factory, account_id=account_id)).db
    assert (
        (
            preview.support_requests_anonymized.count,
            preview.support_person_limits.count,
            preview.support_settlers_cleared.count,
        )
        == (
            report.support_requests_anonymized,
            report.support_person_limits,
            report.support_settlers_cleared,
        )
        == (1, 2, 1)
    ), "the preview names exactly what the purge does"
    rows = await _rows(db_session)
    assert [r.platform_user_id for r in rows] == [ledger.PURGED_USER, STAY], "limit rows remain"
