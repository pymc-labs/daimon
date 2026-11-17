"""Support plans on real Postgres: plans, allowances, the ledger's submit and settle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as plans
from daimon.core.stores.support_rows import SupportCatalogRow, SupportPlanRow
from daimon.core.support_plans import (
    PlanScope,
    RequestChoice,
    SupportCatalogSpec,
    SupportPlace,
    parse_catalog,
)
from daimon.core.windows import WindowBounds
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

D = Decimal
PLACE = SupportPlace(channel_id="C1", agent_id="a1")
TOTAL = WindowBounds("total", None, None)
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
            {"key": "custom", "label": "Custom", "price": {"kind": "quoted"}},
        ],
    }
)


async def _menu(session: AsyncSession, tenant_id: uuid.UUID) -> SupportCatalogRow:
    return await plans.set_catalog(
        session, tenant_id=tenant_id, key="support", spec=MENU, set_by_account_id=None
    )


async def _plan(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    catalog: SupportCatalogRow | None = None,
    scope: PlanScope = "workspace",
    scope_id: str = "",
    pool: Decimal | None = None,
    per_person: Decimal | None = None,
    window: WindowBounds = TOTAL,
) -> SupportPlanRow:
    return await plans.set_plan(
        session,
        tenant_id=tenant_id,
        platform="slack",
        scope=scope,
        scope_id=scope_id,
        catalog_id=None if catalog is None else catalog.id,
        pool=pool,
        per_person=per_person,
        window=window,
        set_by_account_id=None,
    )


async def _submit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    choice: RequestChoice | None = None,
    *,
    user: str = "U1",
    message: str = "m1",
    exempt: bool = False,
    allowance: int = 20,
    now: datetime | None = None,
) -> ledger.SubmitOutcome:
    return await ledger.submit_request(
        session,
        tenant_id=tenant_id,
        account_id=None,
        platform="slack",
        platform_user_id=user,
        channel_id="C1",
        message_id=message,
        ma_session_id=None,
        place=PLACE,
        choice=choice or RequestChoice.default("help"),
        default_allowance=allowance,
        exempt=exempt,
        now=now,
    )


async def _settle(
    session: AsyncSession,
    escalation_id: uuid.UUID,
    charge: Decimal | None,
    expected: Decimal | None = None,
) -> ledger.SettleOutcome:
    return await ledger.settle_request(
        session,
        escalation_id=escalation_id,
        charge=charge,
        settled_by="staff",
        settled_by_account_id=None,
        default_allowance=20,
        expected_charge=expected,
    )


async def _legacy(session: AsyncSession, tenant_id: uuid.UUID, message: str) -> None:
    row = await ledger.record_escalation(
        session,
        tenant_id=tenant_id,
        account_id=None,
        platform="slack",
        platform_user_id="U1",
        channel_id="C1",
        message_id=message,
        ma_session_id=None,
        note="help",
        allowance=20,
    )
    assert row is not None, "the legacy path still records"


async def test_default_spent_when_legacy_rows_then_equals_their_count(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    for message in ("m1", "m2"):
        await _legacy(db_session, tenant.id, message)
    outcome = await _submit(db_session, tenant.id, message="m3")
    assert outcome.status == "recorded" and outcome.row, "the default plan records"
    assert (outcome.row.charged_to, outcome.row.charge, outcome.row.status) == (
        "default",
        D(1),
        "settled",
    ), "a default request is charged 1 at submit"
    spent = await ledger.default_spent(db_session, tenant_id=tenant.id, platform_user_id="U1")
    count = await ledger.count_escalations_for_user(
        db_session, tenant_id=tenant.id, platform_user_id="U1"
    )
    assert (spent, count) == (D(3), 3), "the sum equals today's count"


async def test_submit_when_default_allowance_spent_then_out_of_credits_unless_raised(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    assert (await _submit(db_session, tenant.id, allowance=1)).status == "recorded"
    late = await _submit(db_session, tenant.id, message="m2", allowance=1)
    assert late.status == "out_of_credits", "the default allowance binds"
    await plans.set_allowance(
        db_session,
        tenant_id=tenant.id,
        plan_id=None,
        platform_user_id="U1",
        credits=D(3),
        set_by_account_id=None,
    )
    raised = await _submit(db_session, tenant.id, message="m2", allowance=1)
    assert raised.status == "recorded" and raised.view, "a default-plan allowance replaces it"
    assert raised.view.available == D(1), "3 allowed, 2 spent"


async def test_submit_when_pool_and_person_limits_then_the_stricter_binds(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D("1.5"), per_person=D(1))
    quick = RequestChoice("quick", menu.revision)
    statuses = [
        (await _submit(db_session, tenant.id, quick, user=user, message=message)).status
        for user, message in (("U1", "m1"), ("U1", "m2"), ("U1", "m3"), ("U2", "m4"), ("U3", "m5"))
    ]
    assert statuses == ["recorded", "recorded", "out_of_credits", "recorded", "out_of_credits"], (
        "U1 stops at their 1 credit, U3 at the 1.5 pool"
    )
    view = await ledger.resolve_plan(
        db_session, tenant_id=tenant.id, place=PLACE, platform_user_id="U3", default_allowance=20
    )
    assert (view.balance.axis, view.available) == ("pool", D(0)), "the pool is the stricter axis"


async def test_submit_when_person_has_an_allowance_then_it_replaces_the_cap(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    plan = await _plan(db_session, tenant.id, per_person=D(1))
    await plans.set_allowance(
        db_session,
        tenant_id=tenant.id,
        plan_id=plan.id,
        platform_user_id="U1",
        credits=D("2.5"),
        set_by_account_id=None,
    )
    view = await ledger.resolve_plan(
        db_session, tenant_id=tenant.id, place=PLACE, platform_user_id="U1", default_allowance=20
    )
    assert view.available == D("2.5"), "the allowance, not the plan's per-person cap"
    other = await ledger.resolve_plan(
        db_session, tenant_id=tenant.id, place=PLACE, platform_user_id="U2", default_allowance=20
    )
    assert other.available == D(1), "everyone else keeps the plan's cap"


async def test_resolve_plan_when_several_scopes_then_the_channel_plan_governs(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _plan(db_session, tenant.id, pool=D(5))
    await _plan(db_session, tenant.id, scope="agent", scope_id="a1", pool=D(4))
    channel = await _plan(db_session, tenant.id, scope="channel", scope_id="C1", pool=D(3))
    view = await ledger.resolve_plan(
        db_session, tenant_id=tenant.id, place=PLACE, platform_user_id="U1", default_allowance=20
    )
    assert view.plan == channel and view.charged_to == "plan", "channel beats agent and workspace"
    assert view.available == D(3), "no stacking across scopes"
    elsewhere = SupportPlace(channel_id="C9", agent_id="a9")
    workspace = await ledger.resolve_plan(
        db_session,
        tenant_id=tenant.id,
        place=elsewhere,
        platform_user_id="U1",
        default_allowance=20,
    )
    assert workspace.available == D(5), "elsewhere the workspace plan governs"


async def test_submit_when_range_option_then_holds_the_minimum_with_no_charge_yet(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D(2))
    outcome = await _submit(db_session, tenant.id, RequestChoice("review", menu.revision))
    assert outcome.status == "recorded" and outcome.row and outcome.view, "recorded"
    row = outcome.row
    assert (row.status, row.hold, row.charge, row.counted) == ("pending", D(1), None, D(1)), (
        "a range request is pending, holding its minimum (decision 3)"
    )
    assert outcome.view.available == D(1), "the view already counts the hold"
    assert row.option and row.option.key == "review", "the terms are snapshotted"


async def test_submit_when_menu_changed_then_stale(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D(2))
    old = await _submit(db_session, tenant.id, RequestChoice("quick", menu.revision - 1))
    gone = await _submit(db_session, tenant.id, RequestChoice("nope", menu.revision))
    default = await _submit(db_session, tenant.id)
    assert [old.status, gone.status, default.status] == ["stale", "stale", "stale"], (
        "an old revision, a removed option or the default menu all read as changed"
    )


async def test_submit_when_same_message_and_option_then_duplicate(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D(5))
    first = await _submit(db_session, tenant.id, RequestChoice("quick", menu.revision))
    again = await _submit(db_session, tenant.id, RequestChoice("quick", menu.revision))
    other = await _submit(db_session, tenant.id, RequestChoice("review", menu.revision))
    assert again.status == "duplicate" and again.row == first.row, "the earlier row comes back"
    assert other.status == "recorded", "another option on the same message is a new request"


async def test_submit_when_window_ended_then_plan_inactive_even_for_an_admin(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    window = WindowBounds(
        "fixed", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    )
    await _plan(db_session, tenant.id, pool=D(5), window=window)
    late = datetime(2026, 3, 1, tzinfo=UTC)
    member = await _submit(db_session, tenant.id, now=late)
    admin = await _submit(db_session, tenant.id, now=late, exempt=True)
    early = await _submit(db_session, tenant.id, now=datetime(2025, 12, 1, tzinfo=UTC))
    assert [member.status, admin.status, early.status] == ["plan_inactive"] * 3, (
        "outside the window nobody asks, and there is no fallback to the default plan"
    )
    assert member.view and member.view.available == 0, "an inactive plan has 0 available"


async def test_submit_when_exempt_then_recorded_settled_and_charged_nowhere(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _plan(db_session, tenant.id, pool=D(0))
    member = await _submit(db_session, tenant.id)
    admin = await _submit(db_session, tenant.id, exempt=True)
    assert member.status == "out_of_credits", "an empty pool refuses a member"
    assert admin.status == "recorded" and admin.row, "an admin is exempt (decision 5)"
    row = admin.row
    assert (row.charged_to, row.hold, row.charge, row.status) == ("exempt", 0, 0, "settled")
    spent = await ledger.default_spent(db_session, tenant_id=tenant.id, platform_user_id="U1")
    assert spent == 0, "an exempt row counts on no ledger"
    settle = await _settle(db_session, row.id, D(1))
    assert settle.status == "invalid", "an exempt request has nothing to settle"


async def test_settle_when_pending_then_charges_once_within_bounds_and_balance(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D(2))
    row = (await _submit(db_session, tenant.id, RequestChoice("review", menu.revision))).row
    assert row, "recorded"
    assert (await _settle(db_session, row.id, D("0.5"))).status == "invalid", "below the min"
    over = await _settle(db_session, row.id, D(3))
    assert (over.status, over.available) == ("over_balance", D(1)), "3 needs 2 more; 1 is left"
    done = await _settle(db_session, row.id, D(2))
    assert done.status == "settled" and done.row, "2 fits the hold plus what is left"
    assert (done.row.charge, done.row.settled_by, done.available) == (D(2), "staff", D(0))
    again = await _settle(db_session, row.id, D(2))
    assert again.status == "already_settled", "a second click changes nothing"
    missing = await _settle(db_session, uuid.uuid4(), D(1))
    assert missing.status == "not_found", "an unknown request"


async def test_settle_when_declined_then_charges_zero_and_frees_the_hold(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D(2))
    row = (await _submit(db_session, tenant.id, RequestChoice("custom", menu.revision))).row
    assert row and row.hold == 0 and row.status == "pending", "a quote with min 0 holds 0"
    declined = await _settle(db_session, row.id, None)
    assert declined.status == "declined" and declined.row, "declined"
    assert (declined.row.charge, declined.row.status) == (D(0), "declined"), "charged 0"


async def test_settle_when_correcting_then_needs_the_charge_it_was_read_at(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D(5))
    row = (await _submit(db_session, tenant.id, RequestChoice("review", menu.revision))).row
    assert row, "recorded"
    pending = await _settle(db_session, row.id, D(0), expected=D(1))
    assert pending.status == "stale", "a pending request is settled, not corrected"
    await _settle(db_session, row.id, D(2))
    stale = await _settle(db_session, row.id, D(0), expected=D(1))
    assert stale.status == "stale", "it is charged 2 now, not 1"
    refund = await _settle(db_session, row.id, D(0), expected=D(2))
    assert refund.status == "settled" and refund.row and refund.row.charge == 0, "refunded to 0"


async def test_settle_when_plan_was_cleared_then_a_charge_is_refused_and_a_decline_goes_through(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D(1))
    row = (await _submit(db_session, tenant.id, RequestChoice("review", menu.revision))).row
    assert row, "recorded"
    await plans.delete_plan(db_session, tenant_id=tenant.id, scope="workspace", scope_id="")
    orphan = await ledger.get_request(db_session, escalation_id=row.id)
    assert orphan and orphan.plan_id is None and orphan.charged_to == "plan", "kept, unlinked"
    refused = await _settle(db_session, row.id, D(3))
    assert refused.status == "plan_gone", "no plan left to bound the charge"
    assert refused.row and refused.row.status == "pending", "the request stays pending"
    declined = await _settle(db_session, row.id, None)
    assert declined.status == "declined", "a decline needs no balance"
    after = await _submit(db_session, tenant.id, message="m2")
    assert after.status == "recorded" and after.view, "the default plan governs again"
    assert after.view.charged_to == "default", "no plan, default ledger"


async def test_settle_when_plan_was_cleared_then_a_correction_may_only_go_down(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    menu = await _menu(db_session, tenant.id)
    await _plan(db_session, tenant.id, catalog=menu, pool=D(5))
    row = (await _submit(db_session, tenant.id, RequestChoice("review", menu.revision))).row
    assert row, "recorded"
    assert (await _settle(db_session, row.id, D(2))).status == "settled", "charged 2"
    await plans.delete_plan(db_session, tenant_id=tenant.id, scope="workspace", scope_id="")
    up = await _settle(db_session, row.id, D(3), expected=D(2))
    assert up.status == "plan_gone", "a raise has no balance to check against"
    down = await _settle(db_session, row.id, D(1), expected=D(2))
    assert down.status == "settled" and down.row and down.row.charge == 1, "a refund still works"


async def test_plan_spent_when_monthly_then_last_months_rows_do_not_count(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    plan = await _plan(db_session, tenant.id, pool=D(1), window=WindowBounds("monthly", None, None))
    row = (await _submit(db_session, tenant.id)).row
    assert row, "recorded"
    old = datetime.now(UTC) - timedelta(days=40)
    await db_session.execute(
        text("UPDATE support_escalations SET created_at = :at WHERE id = :id"),
        {"at": old, "id": row.id},
    )
    now = datetime.now(UTC)
    assert await ledger.plan_spent(db_session, plan=plan, platform_user_id="U1", as_of=now) == (
        D(0),
        D(0),
    ), "a new month starts empty"
    then = await ledger.plan_spent(db_session, plan=plan, platform_user_id="U1", as_of=old)
    assert then == (D(1), D(1)), "the old month still holds its row"


async def test_set_catalog_when_set_again_then_revision_rises_tenant_wide(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    first = await _menu(db_session, tenant.id)
    other = await plans.set_catalog(
        db_session, tenant_id=tenant.id, key="other", spec=MENU, set_by_account_id=None
    )
    again = await _menu(db_session, tenant.id)
    assert first.revision < other.revision < again.revision, "every set takes a new number"
    assert again.id == first.id and again.spec == MENU, "the same menu, updated in place"


async def test_set_catalog_when_deleted_and_recreated_then_an_open_menu_is_stale(
    db_session: AsyncSession,
) -> None:
    """A removed menu's revision is never handed out again, so an old submit cannot pass."""
    tenant = await make_tenant(db_session, platform="slack")

    def menu(price: str) -> SupportCatalogSpec:
        option = {"key": "x", "label": "X", "price": {"kind": "fixed", "amount": price}}
        return parse_catalog({"name": "M", "options": [option]})

    async def put(key: str, price: str) -> SupportCatalogRow:
        return await plans.set_catalog(
            db_session, tenant_id=tenant.id, key=key, spec=menu(price), set_by_account_id=None
        )

    first = await put("a", "1")
    doomed = await put("b", "1")
    await _plan(db_session, tenant.id, catalog=doomed, pool=D(10))
    shown = await ledger.resolve_plan(
        db_session, tenant_id=tenant.id, place=PLACE, platform_user_id="U1", default_allowance=20
    )
    await _plan(db_session, tenant.id, catalog=first, pool=D(10))
    await plans.delete_catalog(db_session, tenant_id=tenant.id, key="b")
    recreated = await put("c", "5")
    await _plan(db_session, tenant.id, catalog=recreated, pool=D(10))
    assert recreated.revision not in {first.revision, shown.revision}, "a number is never reused"
    outcome = await _submit(db_session, tenant.id, RequestChoice("x", shown.revision))
    assert outcome.status == "stale", (
        f"accepted at {outcome.row and outcome.row.charge} after seeing price 1"
    )
