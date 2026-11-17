"""Support limits on real Postgres: resolution, the ledger's submit and settle, menus."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.support_rows import LimitView, SupportLimitRow, SupportMenuRow
from daimon.core.support import (
    DEFAULT_OPTION_KEY,
    MenuSpec,
    RequestChoice,
    SupportPlace,
    SupportScope,
    SupportWindow,
    parse_menu,
)
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

D = Decimal
PLACE = SupportPlace(channel_id="C1", agent_id="a1")
MENU = parse_menu(
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


def _choice(key: str, spec: MenuSpec = MENU) -> RequestChoice:
    """A pick of `key` as `spec` shows it."""
    option = spec.option(key)
    assert option is not None, f"{key} is on the menu"
    return RequestChoice.of(option)


async def _menu(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    key: str = "support",
    spec: MenuSpec = MENU,
    scope: SupportScope = "workspace",
    scope_id: str = "",
) -> SupportMenuRow:
    """Set a menu and use it at one place (everywhere by default)."""
    menu = await store.set_menu(
        session, tenant_id=tenant_id, key=key, spec=spec, set_by_account_id=None
    )
    await store.set_menu_use(
        session,
        tenant_id=tenant_id,
        scope=scope,
        scope_id=scope_id,
        menu_id=menu.id,
        set_by_account_id=None,
    )
    return menu


async def _limit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    scope: SupportScope = "workspace",
    scope_id: str = "",
    person: str | None = None,
    shared: Decimal | None = None,
    each: Decimal | None = None,
    window: SupportWindow = "total",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
) -> SupportLimitRow:
    return await store.set_limit(
        session,
        tenant_id=tenant_id,
        scope=scope,
        scope_id=scope_id,
        platform_user_id=person,
        shared=shared,
        each_person=each,
        window=window,
        starts_at=starts_at,
        ends_at=ends_at,
        set_by_account_id=None,
    )


async def _view(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    user: str = "U1",
    place: SupportPlace = PLACE,
    exempt: bool = False,
) -> LimitView:
    return await ledger.resolve_limit(
        session,
        tenant_id=tenant_id,
        place=place,
        platform_user_id=user,
        default_allowance=20,
        exempt=exempt,
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
    channel: str = "C1",
    place: SupportPlace = PLACE,
) -> ledger.SubmitOutcome:
    return await ledger.submit_request(
        session,
        tenant_id=tenant_id,
        account_id=None,
        platform="slack",
        platform_user_id=user,
        channel_id=channel,
        message_id=message,
        ma_session_id=None,
        place=place,
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
    assert outcome.status == "recorded" and outcome.row, "the default ledger records"
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
    await _limit(db_session, tenant.id, person="U1", each=D(3))
    raised = await _submit(db_session, tenant.id, message="m2", allowance=1)
    assert raised.status == "recorded" and raised.view, "a person's own limit replaces it"
    assert (raised.view.charged_to, raised.view.available) == ("default", D(1)), "3 allowed"


async def test_submit_when_shared_and_each_person_limits_then_the_stricter_binds(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D("1.5"), each=D(1))
    quick = _choice("quick")
    statuses = [
        (await _submit(db_session, tenant.id, quick, user=user, message=message)).status
        for user, message in (("U1", "m1"), ("U1", "m2"), ("U1", "m3"), ("U2", "m4"), ("U3", "m5"))
    ]
    assert statuses == ["recorded", "recorded", "out_of_credits", "recorded", "out_of_credits"], (
        "U1 stops at their 1 credit, U3 at the 1.5 shared"
    )
    view = await _view(db_session, tenant.id, user="U3")
    assert (view.balance.axis, view.available) == ("shared", D(0)), "shared is the stricter axis"


async def test_resolve_when_a_person_has_their_own_limit_then_it_replaces_the_cap(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _limit(db_session, tenant.id, shared=D(10), each=D(1))
    await _limit(db_session, tenant.id, person="U1", each=D("2.5"))
    view = await _view(db_session, tenant.id)
    assert view.available == D("2.5"), "their own limit, not the place's each-person cap"
    assert (await _view(db_session, tenant.id, user="U2")).available == D(1), "others keep it"


async def test_resolve_when_several_scopes_then_the_most_specific_limit_is_charged(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _limit(db_session, tenant.id, shared=D(5))
    agent = await _limit(db_session, tenant.id, scope="agent", scope_id="a1", shared=D(4))
    channel = await _limit(db_session, tenant.id, scope="channel", scope_id="C1", shared=D(3))
    view = await _view(db_session, tenant.id)
    assert view.limit == channel and view.charged_to == "limit", "channel beats agent, workspace"
    assert view.available == D(3), "no stacking across scopes"
    other_channel = await _view(db_session, tenant.id, place=SupportPlace("C9", "a1"))
    assert other_channel.limit == agent, "then the agent"
    elsewhere = await _view(db_session, tenant.id, place=SupportPlace("C9", "a9"))
    assert elsewhere.available == D(5), "elsewhere the workspace limit governs"


async def test_resolve_when_a_person_row_is_in_one_place_then_it_beats_everywhere(
    db_session: AsyncSession,
) -> None:
    """A person's row caps them under whichever place is charged; one place beats Everywhere."""
    tenant = await make_tenant(db_session, platform="slack")
    await _limit(db_session, tenant.id, scope="agent", scope_id="a1", shared=D(10), each=D(1))
    await _limit(db_session, tenant.id, person="U1", each=D(2))
    vip = await _limit(
        db_session, tenant.id, scope="channel", scope_id="C1", person="U1", each=D(4)
    )
    here = await _view(db_session, tenant.id)
    assert (here.limit and here.limit.scope, here.person) == ("agent", vip), "the agent is charged"
    assert here.available == D(4), "capped by their row in this channel"
    elsewhere = await _view(db_session, tenant.id, place=SupportPlace("C9", "a1"))
    assert elsewhere.available == D(2), "Everywhere applies outside the channel"


async def test_resolve_when_only_a_person_row_then_it_adds_no_credits_to_the_place(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _limit(db_session, tenant.id, scope="channel", scope_id="C1", person="U1", each=D(2))
    view = await _view(db_session, tenant.id)
    assert (view.limit, view.charged_to, view.available) == (None, "default", D(2)), (
        "the default ledger, capped at their 2"
    )
    other = await _view(db_session, tenant.id, user="U2")
    assert (other.person, other.available) == (None, D(20)), "everyone else: the default"


async def test_resolve_when_menus_are_set_per_place_then_independent_of_limits(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    everywhere = await _menu(db_session, tenant.id, key="general")
    vip = await _menu(db_session, tenant.id, key="vip", scope="channel", scope_id="C1")
    await _limit(db_session, tenant.id, scope="agent", scope_id="a1", shared=D(5))
    here = await _view(db_session, tenant.id)
    assert (here.menu, here.limit and here.limit.scope) == (vip, "agent"), (
        "channel menu, agent limit"
    )
    assert (await _view(db_session, tenant.id, place=SupportPlace("C9", "a9"))).menu == everywhere
    await store.clear_menu_use(db_session, tenant_id=tenant.id, scope="workspace", scope_id="")
    built_in = await _view(db_session, tenant.id, place=SupportPlace("C9", "a9"))
    assert built_in.menu is None and built_in.spec.options[0].key == DEFAULT_OPTION_KEY


async def test_submit_when_range_option_then_holds_the_minimum_with_no_charge_yet(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D(2))
    outcome = await _submit(db_session, tenant.id, _choice("review"))
    assert outcome.status == "recorded" and outcome.row and outcome.view, "recorded"
    row = outcome.row
    assert (row.status, row.hold, row.charge, row.counted) == ("pending", D(1), None, D(1)), (
        "a range request is pending, holding its minimum (decision 3)"
    )
    assert outcome.view.available == D(1), "the view already counts the hold"
    assert row.option and row.option.key == "review", "the terms are snapshotted"


async def test_submit_when_menu_changed_then_stale(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D(2))
    old = await _submit(db_session, tenant.id, RequestChoice("quick", "older terms"))
    gone = await _submit(db_session, tenant.id, RequestChoice("nope", _choice("quick").terms))
    default = await _submit(db_session, tenant.id)
    assert [old.status, gone.status, default.status] == ["stale", "stale", "stale"], (
        "other terms, a removed option or the default menu all read as changed"
    )


async def test_submit_when_same_message_and_option_then_duplicate(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D(5))
    first = await _submit(db_session, tenant.id, _choice("quick"))
    again = await _submit(db_session, tenant.id, _choice("quick"))
    other = await _submit(db_session, tenant.id, _choice("review"))
    assert again.status == "duplicate" and again.row == first.row, "the earlier row comes back"
    assert other.status == "recorded", "another option on the same message is a new request"


async def test_asked_options_when_options_and_a_legacy_row_then_keys_per_message(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _legacy(db_session, tenant.id, "m0")
    await _limit(db_session, tenant.id, shared=D(5))
    await _submit(db_session, tenant.id, _choice("quick"))
    await _submit(db_session, tenant.id, _choice("review"))
    await _submit(db_session, tenant.id, _choice("quick"), message="m2")

    async def asked(message: str, user: str = "U1") -> frozenset[str]:
        return await ledger.asked_options(
            db_session,
            tenant_id=tenant.id,
            platform="slack",
            platform_user_id=user,
            channel_id="C1",
            message_id=message,
        )

    assert await asked("m1") == {"quick", "review"}, "each option asked on the message"
    assert await asked("m2") == {"quick"}, "per message"
    assert await asked("m1", user="U2") == frozenset(), "per person"
    assert await asked("m0") == {DEFAULT_OPTION_KEY}, "a legacy row is the default option"


async def test_submit_when_outside_the_dates_then_limit_inactive_even_for_an_admin(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    jan, feb = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC)
    await _limit(db_session, tenant.id, shared=D(5), starts_at=jan, ends_at=feb)
    late = datetime(2026, 3, 1, tzinfo=UTC)
    member = await _submit(db_session, tenant.id, now=late)
    admin = await _submit(db_session, tenant.id, now=late, exempt=True)
    early = await _submit(db_session, tenant.id, now=datetime(2025, 12, 1, tzinfo=UTC))
    assert [member.status, admin.status, early.status] == ["limit_inactive"] * 3, (
        "outside the dates nobody asks, and there is no fallback to the default ledger"
    )
    assert member.view and member.view.available == 0, "an inactive limit has 0 available"


async def test_submit_when_the_persons_own_limit_ended_then_limit_inactive(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _limit(db_session, tenant.id, shared=D(5))
    ended = datetime.now(UTC) - timedelta(days=1)
    await _limit(db_session, tenant.id, person="U1", each=D(2), ends_at=ended)
    assert (await _submit(db_session, tenant.id)).status == "limit_inactive", "their row ended"
    other = await _submit(db_session, tenant.id, user="U2")
    assert other.status == "recorded", "the place limit still serves everyone else"


async def test_submit_when_exempt_then_recorded_settled_and_charged_nowhere(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _limit(db_session, tenant.id, shared=D(0))
    member = await _submit(db_session, tenant.id)
    admin = await _submit(db_session, tenant.id, exempt=True)
    assert member.status == "out_of_credits", "no shared credits refuses a member"
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
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D(2))
    row = (await _submit(db_session, tenant.id, _choice("review"))).row
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
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D(2))
    row = (await _submit(db_session, tenant.id, _choice("custom"))).row
    assert row and row.hold == 0 and row.status == "pending", "a quote with min 0 holds 0"
    declined = await _settle(db_session, row.id, None)
    assert declined.status == "declined" and declined.row, "declined"
    assert (declined.row.charge, declined.row.status) == (D(0), "declined"), "charged 0"


async def test_settle_when_correcting_then_needs_the_charge_it_was_read_at(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D(5))
    row = (await _submit(db_session, tenant.id, _choice("review"))).row
    assert row, "recorded"
    pending = await _settle(db_session, row.id, D(0), expected=D(1))
    assert pending.status == "stale", "a pending request is settled, not corrected"
    await _settle(db_session, row.id, D(2))
    stale = await _settle(db_session, row.id, D(0), expected=D(1))
    assert stale.status == "stale", "it is charged 2 now, not 1"
    refund = await _settle(db_session, row.id, D(0), expected=D(2))
    assert refund.status == "settled" and refund.row and refund.row.charge == 0, "refunded to 0"


async def test_settle_when_limit_was_cleared_then_a_charge_is_refused_and_a_decline_goes_through(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    limit = await _limit(db_session, tenant.id, shared=D(1))
    row = (await _submit(db_session, tenant.id, _choice("review"))).row
    assert row, "recorded"
    await store.delete_limit(db_session, tenant_id=tenant.id, limit_id=limit.id)
    orphan = await ledger.get_request(db_session, escalation_id=row.id)
    assert orphan and orphan.limit_id is None and orphan.charged_to == "limit", "kept, unlinked"
    refused = await _settle(db_session, row.id, D(3))
    assert refused.status == "limit_gone", "no limit left to bound the charge"
    assert refused.row and refused.row.status == "pending", "the request stays pending"
    declined = await _settle(db_session, row.id, None)
    assert declined.status == "declined", "a decline needs no balance"
    after = await _submit(db_session, tenant.id, _choice("quick"), message="m2")
    assert after.status == "recorded" and after.view, "the default ledger governs again"
    assert after.view.charged_to == "default", "no limit, default ledger"


async def test_settle_when_limit_was_cleared_then_a_correction_may_only_go_down(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    limit = await _limit(db_session, tenant.id, shared=D(5))
    row = (await _submit(db_session, tenant.id, _choice("review"))).row
    assert row, "recorded"
    assert (await _settle(db_session, row.id, D(2))).status == "settled", "charged 2"
    await store.delete_limit(db_session, tenant_id=tenant.id, limit_id=limit.id)
    up = await _settle(db_session, row.id, D(3), expected=D(2))
    assert up.status == "limit_gone", "a raise has no balance to check against"
    down = await _settle(db_session, row.id, D(1), expected=D(2))
    assert down.status == "settled" and down.row and down.row.charge == 1, "a refund still works"


async def test_limit_spent_when_monthly_then_last_months_rows_do_not_count(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    limit = await _limit(db_session, tenant.id, shared=D(1), window="monthly")
    row = (await _submit(db_session, tenant.id)).row
    assert row, "recorded"
    old = datetime.now(UTC) - timedelta(days=40)
    await db_session.execute(
        text("UPDATE support_escalations SET created_at = :at WHERE id = :id"),
        {"at": old, "id": row.id},
    )
    now = datetime.now(UTC)
    assert await ledger.limit_spent(db_session, limit=limit, platform_user_id="U1", as_of=now) == (
        D(0),
        D(0),
    ), "a new month starts empty"
    then = await ledger.limit_spent(db_session, limit=limit, platform_user_id="U1", as_of=old)
    assert then == (D(1), D(1)), "the old month still holds its row"


async def test_set_menu_when_set_again_then_revision_rises_tenant_wide(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    first = await _menu(db_session, tenant.id)
    other = await store.set_menu(
        db_session, tenant_id=tenant.id, key="other", spec=MENU, set_by_account_id=None
    )
    again = await _menu(db_session, tenant.id)
    assert first.revision < other.revision < again.revision, "every set takes a new number"
    assert again.id == first.id and again.spec == MENU, "the same menu, updated in place"


async def test_set_menu_when_deleted_and_recreated_then_an_open_menu_is_stale(
    db_session: AsyncSession,
) -> None:
    """A menu removed and re-added at another price never passes an old pick (ABA)."""
    tenant = await make_tenant(db_session, platform="slack")

    def menu(price: str) -> MenuSpec:
        option = {"key": "x", "label": "X", "price": {"kind": "fixed", "amount": price}}
        return parse_menu({"name": "M", "options": [option]})

    await _limit(db_session, tenant.id, shared=D(10))
    first = await _menu(db_session, tenant.id, key="a", spec=menu("1"))
    shown = await _view(db_session, tenant.id)
    await store.clear_menu_use(db_session, tenant_id=tenant.id, scope="workspace", scope_id="")
    await store.delete_menu(db_session, tenant_id=tenant.id, key="a")
    recreated = await _menu(db_session, tenant.id, key="a", spec=menu("5"))
    assert recreated.revision != first.revision, "a number is never reused"
    outcome = await _submit(db_session, tenant.id, _choice("x", shown.spec))
    assert outcome.status == "stale", (
        f"accepted at {outcome.row and outcome.row.charge} after seeing price 1"
    )


async def test_submit_when_only_the_label_changed_then_recorded_on_the_current_terms(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D(5))
    relabelled = MENU.model_copy(
        update={
            "options": (MENU.options[0].model_copy(update={"label": "Fast"}), *MENU.options[1:])
        }
    )
    await store.set_menu(
        db_session, tenant_id=tenant.id, key="support", spec=relabelled, set_by_account_id=None
    )
    outcome = await _submit(db_session, tenant.id, _choice("quick"))
    assert outcome.status == "recorded" and outcome.row, f"refused: {outcome.status}"
    assert outcome.row.option and outcome.row.option.label == "Fast", "the label as it is now"


async def test_submit_when_the_picked_price_or_fields_changed_then_stale(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, shared=D(5))
    quick, review, custom = MENU.options
    field = {"key": "why", "kind": "text", "label": "Why"}
    changed = MENU.model_copy(
        update={
            "options": (
                quick.model_copy(update={"price": review.price}),
                review,
                MenuSpec.model_validate(
                    {"name": "x", "options": [{**custom.model_dump(), "fields": [field]}]}
                ).options[0],
            )
        }
    )
    await store.set_menu(
        db_session, tenant_id=tenant.id, key="support", spec=changed, set_by_account_id=None
    )
    statuses = [
        (await _submit(db_session, tenant.id, _choice(key), message=key)).status
        for key in ("quick", "custom", "review")
    ]
    assert statuses == ["stale", "stale", "recorded"], (
        "a new price or a new field is stale; an unchanged option still goes through"
    )


async def test_settle_when_the_person_row_capped_the_request_then_it_caps_the_charge(
    db_session: AsyncSession,
) -> None:
    """An agent's person row on a default-ledger request, and a channel's under a thread."""
    tenant = await make_tenant(db_session, platform="slack")
    await _menu(db_session, tenant.id)
    await _limit(db_session, tenant.id, scope="agent", scope_id="a1", person="U1", each=D(2))
    default = (await _submit(db_session, tenant.id, _choice("review"))).row
    assert default and default.charged_to == "default", "no place limit: the default ledger"
    over = await _settle(db_session, default.id, D(3))
    assert (over.status, over.available) == ("over_balance", D(1)), "their 2, not the default 20"

    await _limit(db_session, tenant.id, shared=D(10))
    await _limit(db_session, tenant.id, scope="channel", scope_id="C1", person="U2", each=D(2))
    thread = await _submit(db_session, tenant.id, _choice("review"), user="U2", channel="T1")
    assert thread.row and thread.row.charged_to == "limit", "a thread under C1"
    capped = await _settle(db_session, thread.row.id, D(3))
    assert (capped.status, capped.available) == ("over_balance", D(1)), "their row in C1"
