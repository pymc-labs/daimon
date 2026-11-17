"""Pure rules for support plans: credits, catalogs, pricing, precedence and copy."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.stores.support_escalation import SubmitOutcome, SubmitStatus
from daimon.core.stores.support_rows import PlanView, SupportEscalationRow
from daimon.core.support_escalation import ALREADY_REQUESTED, OUT_OF_CREDITS, UNAVAILABLE
from daimon.core.support_plans import (
    CLIENT_NOT_NOTIFIED,
    DEFAULT_CATALOG,
    EXEMPT_RECEIVED,
    NOT_ENOUGH_CREDITS,
    OPTIONS_CHANGED,
    Balance,
    ChargedTo,
    PlanScope,
    PriceBounds,
    QuotedPrice,
    RequestStatus,
    SupportCatalogSpec,
    SupportPlace,
    SupportPlanError,
    balance_text,
    cheapest_bounds,
    format_credits,
    governing_plan,
    hold_for,
    inactive_text,
    load_catalog,
    may_submit,
    option_text,
    option_value,
    parse_amount,
    parse_catalog,
    parse_credits,
    parse_plan_spec,
    picked_option,
    post_status_text,
    price_bounds,
    price_text,
    received_text,
    refusal_text,
    request_note,
    settle_problem,
    settle_range_text,
    settled_text,
    settles_at_submit,
    snapshot_option,
    validate_answers,
)
from daimon.core.windows import WindowBounds

D = Decimal

STEPS = {
    "kind": "steps",
    "label": "Length",
    "steps": [{"label": "30 min", "amount": "0.5"}, {"label": "1 hour", "amount": "1"}],
}
CATALOG = {
    "name": "Support",
    "options": [
        {"key": "quick", "label": "Quick question", "price": {"kind": "fixed", "amount": "0.5"}},
        {
            "key": "call",
            "label": "Call",
            "price": STEPS,
            "fields": [{"key": "topic", "kind": "text", "label": "Topic"}],
        },
        {"key": "review", "label": "Review", "price": {"kind": "range", "min": "1", "max": "3"}},
        {
            "key": "custom",
            "label": "Custom",
            "price": {"kind": "quoted"},
            "fields": [
                {"key": "what", "kind": "paragraph", "label": "What"},
                {"key": "size", "kind": "choice", "label": "Size", "choices": ["S", "L"]},
                {"key": "extra", "kind": "text", "label": "Extra", "required": False},
            ],
        },
    ],
}


@pytest.mark.parametrize("value", ["0.5", "1.25", 2, D("0"), "100000"])
def test_parse_credits_when_valid_then_keeps_the_exact_amount(value: object) -> None:
    assert parse_credits(value) == D(str(value)), "credits are exact decimals"


@pytest.mark.parametrize("value", [0.5, True, "1.234", "-1", "100001", "abc", "NaN", None])
def test_parse_credits_when_float_or_out_of_range_then_refuses(value: object) -> None:
    with pytest.raises(SupportPlanError):
        parse_credits(value)


def test_parse_amount_when_zero_then_refuses_unless_allowed() -> None:
    with pytest.raises(SupportPlanError):
        parse_amount("0")
    assert parse_amount("0", allow_zero=True) == 0, "a quoted min may be 0"
    with pytest.raises(SupportPlanError):
        parse_amount("1001")


def test_format_credits_when_trailing_zeros_then_drops_them() -> None:
    assert [format_credits(D(v)) for v in ("1.00", "0.50", "100", "1.25")] == [
        "1",
        "0.5",
        "100",
        "1.25",
    ], "no trailing zeros and no exponent"


def test_parse_catalog_when_valid_then_round_trips_as_decimal_strings() -> None:
    spec = parse_catalog(CATALOG)
    dumped = spec.model_dump(mode="json")
    assert dumped["options"][0]["price"]["amount"] == "0.5", "amounts serialize as strings"
    assert parse_catalog(dumped) == spec, "a stored catalog parses back unchanged"
    assert load_catalog("name: S\noptions:\n- {key: a, label: A, price: {kind: quoted}}\n")


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"options": []}, "options"),
        ({"options": [CATALOG["options"][0], CATALOG["options"][0]]}, "option keys"),
        (
            {"options": [{"key": "a", "label": "A", "price": {"kind": "fixed", "amount": 0.5}}]},
            "decimal string",
        ),
        (
            {
                "options": [
                    {"key": "a", "label": "A", "price": {"kind": "range", "min": 2, "max": 1}}
                ]
            },
            "min below max",
        ),
        (
            {"options": [{"key": "A!", "label": "A", "price": {"kind": "quoted"}}]},
            'option "A!", key',
        ),
    ],
)
def test_parse_catalog_when_invalid_then_names_the_problem(
    change: dict[str, object], says: str
) -> None:
    with pytest.raises(SupportPlanError) as caught:
        parse_catalog({**CATALOG, **change})
    assert says in str(caught.value), f"the error should name the bad entry: {caught.value}"


def test_parse_catalog_when_long_text_field_then_names_it_and_points_to_paragraph() -> None:
    field = {"key": "details", "kind": "text", "label": "Details", "max_chars": 3000}
    review = {"key": "review", "label": "Review", "price": {"kind": "quoted"}, "fields": [field]}
    with pytest.raises(SupportPlanError) as caught:
        parse_catalog({**CATALOG, "options": [CATALOG["options"][0], review]})
    said = str(caught.value)
    assert 'option "review", field "details", max_chars' in said, f"names by key: {said}"
    assert "use a paragraph field" in said, f"says what to use instead: {said}"


def test_parse_catalog_when_steps_and_four_fields_then_refuses_as_too_many() -> None:
    fields = [{"key": f"f{i}", "kind": "text", "label": f"F{i}"} for i in range(4)]
    call = {"key": "call", "label": "Call", "price": STEPS, "fields": fields}
    with pytest.raises(SupportPlanError) as caught:
        parse_catalog({**CATALOG, "options": [call]})
    assert "at most 4 fields" in str(caught.value), "a steps price takes one modal slot"


def test_load_catalog_when_not_yaml_then_names_the_line_and_column() -> None:
    with pytest.raises(SupportPlanError) as caught:
        load_catalog('name: S\noptions: [{"key": "a",, }]\n')
    assert "at line 2, column" in str(caught.value), f"names where it broke: {caught.value}"


def test_price_bounds_when_each_kind_then_gives_lower_and_upper() -> None:
    spec = parse_catalog(CATALOG)
    quick, call, review, custom = (spec.option(k) for k in ("quick", "call", "review", "custom"))
    assert quick and call and review and custom, "every option is found by key"
    assert price_bounds(quick.price) == PriceBounds(D("0.5"), D("0.5")), "fixed"
    assert price_bounds(call.price, step=1) == PriceBounds(D(1), D(1)), "the picked step"
    assert price_bounds(review.price) == PriceBounds(D(1), D(3)), "range"
    assert price_bounds(custom.price) == PriceBounds(D(0), None), "open quote"
    assert settles_at_submit(call.price) and not settles_at_submit(review.price), "settle at submit"
    with pytest.raises(SupportPlanError):
        price_bounds(call.price)
    with pytest.raises(SupportPlanError):
        price_bounds(quick.price, step=0)


def test_hold_for_when_range_then_holds_the_lower_bound() -> None:
    assert hold_for(PriceBounds(D(1), D(3))) == 1, "decision 3: hold the minimum"


def test_may_submit_when_quoted_min_zero_then_still_needs_some_credit() -> None:
    open_quote = PriceBounds(D(0), None)
    assert not may_submit(open_quote, D(0)), "a 0 balance cannot open a quote"
    assert may_submit(open_quote, D("0.01")), "any credit left can"
    assert may_submit(PriceBounds(D(1), D(1)), None), "unbounded always may"
    assert not may_submit(PriceBounds(D(1), D(1)), D("0.99")), "below the lower bound"


def test_settle_problem_when_out_of_bounds_or_over_balance_then_says_which() -> None:
    bounds = PriceBounds(D(1), D(3))
    ok = settle_problem(bounds, charge=D(2), counted=D(1), available=D(1))
    assert ok is None, "the hold plus what is left covers 2"
    over = settle_problem(bounds, charge=D(3), counted=D(1), available=D(1))
    assert over == "over_balance", "3 needs 2 more than the hold"
    assert settle_problem(bounds, charge=D("0.5"), counted=D(1), available=None) == "invalid"
    refund = settle_problem(bounds, charge=D(0), counted=D(2), available=D(0), correction=True)
    assert refund is None, "a correction may refund down to 0"


def test_settle_range_text_when_balance_is_short_then_caps_the_upper() -> None:
    text = settle_range_text(PriceBounds(D(1), D(3)), counted=D(1), available=D("0.5"))
    assert text == "Enter an amount from 1 to 1.5.", text
    open_text = settle_range_text(PriceBounds(D(0), None), counted=D(0), available=None)
    assert open_text == "Enter at least 0.", open_text


def test_validate_answers_when_fields_checked_then_trims_and_refuses_bad_input() -> None:
    custom = parse_catalog(CATALOG).option("custom")
    assert custom, "custom option exists"
    clean = validate_answers(custom, {"what": "  fix it ", "size": "L"})
    assert clean == {"what": "fix it", "size": "L"}, "trimmed, optional field omitted"
    for bad in ({"size": "L"}, {"what": "x", "size": "M"}, {"what": "x", "size": "S", "no": "1"}):
        with pytest.raises(SupportPlanError):
            validate_answers(custom, bad)


def test_request_note_when_default_option_then_is_the_note_as_is() -> None:
    option = DEFAULT_CATALOG.options[0]
    assert request_note(option, {"note": "help"}) == "help", "today's note is unchanged"
    snap = snapshot_option(option, step=None)
    assert snap.bounds == PriceBounds(D(1), D(1)), "the default request costs 1"


@dataclass(frozen=True)
class _Plan:
    scope: PlanScope
    scope_id: str


def test_governing_plan_when_several_scopes_then_channel_beats_agent_beats_workspace() -> None:
    plans = [_Plan("workspace", ""), _Plan("agent", "a1"), _Plan("channel", "c1")]
    place = SupportPlace(channel_id="c1", agent_id="a1")
    assert governing_plan(plans, place) == plans[2], "channel first"
    assert governing_plan(plans, SupportPlace("c2", "a1")) == plans[1], "then agent"
    assert governing_plan(plans, SupportPlace("c2", None)) == plans[0], "then workspace"
    assert governing_plan(plans[1:], SupportPlace("c2", "a2")) is None, "else the default"


def test_support_place_when_thread_then_rolls_up_to_the_parent() -> None:
    place = SupportPlace.of_thread(channel_id="t1", budget_channel_id="c1", agent_id=None)
    assert place.channel_id == "c1", "threads use their parent channel's plan"


def test_parse_plan_spec_when_valid_then_parses_every_part() -> None:
    spec = parse_plan_spec(scope="channel", scope_id=" c1 ", catalog="default", pool="2")
    assert (spec.scope_id, spec.catalog, spec.pool, spec.per_person) == ("c1", None, D(2), None)
    assert spec.window == WindowBounds("total", None, None), "total by default"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "workspace", "scope_id": "x", "pool": "1"},
        {"scope": "channel", "pool": "1"},
        {"scope": "team", "scope_id": "x", "pool": "1"},
        {"scope": "workspace"},
        {"scope": "workspace", "pool": 1.5},
        {"scope": "workspace", "pool": "1", "window": "fixed"},
        {"scope": "workspace", "pool": "1", "catalog": "Bad Key"},
    ],
)
def test_parse_plan_spec_when_invalid_then_refuses(kwargs: dict[str, object]) -> None:
    with pytest.raises(SupportPlanError):
        parse_plan_spec(**kwargs)  # type: ignore[arg-type]


def test_balance_when_both_axes_then_the_stricter_one_is_available() -> None:
    balance = Balance(pool=D(10), pool_used=D(9), cap=D(2), person_used=D(0))
    assert (balance.axis, balance.available) == ("pool", D(1)), "the pool is stricter"
    person = Balance(pool=D(10), pool_used=D(0), cap=D(2), person_used=D("1.5"))
    assert (person.axis, person.available) == ("person", D("0.5")), "the person is stricter"
    unbounded = Balance(pool=None, pool_used=D(5), cap=None, person_used=D(5))
    assert unbounded.available is None and balance_text(unbounded) is None, "no limit"
    over = Balance(pool=D(1), pool_used=D(3), cap=None, person_used=D(0))
    assert over.available == 0, "a lowered limit never goes negative"


def test_balance_text_when_fixed_window_then_names_the_end() -> None:
    balance = Balance(pool=D(2), pool_used=D("0.5"), cap=None, person_used=D(0))
    window = WindowBounds(
        "fixed", datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 18, tzinfo=UTC)
    )
    text = balance_text(balance, window)
    assert text == "1.5 of 2 team credits left, until 17 Dec", "the last day the end includes"
    one = balance_text(Balance(pool=None, pool_used=D(0), cap=D(1), person_used=D(0)))
    assert one == "1 of 1 credit left", one


def test_inactive_text_when_outside_the_window_then_says_when() -> None:
    window = WindowBounds(
        "fixed", datetime(2026, 11, 18, tzinfo=UTC), datetime(2026, 12, 18, tzinfo=UTC)
    )
    before = inactive_text(window, now=datetime(2026, 11, 1, tzinfo=UTC))
    after = inactive_text(window, now=datetime(2026, 12, 20, tzinfo=UTC))
    assert before == "Support starts on 18 Nov.", before
    assert after == "Support ended on 17 Dec.", "its last included day"
    assert inactive_text(window, now=datetime(2026, 12, 1, tzinfo=UTC)) is None, "active"


def _request(
    charged_to: ChargedTo, status: RequestStatus, hold: Decimal, charge: Decimal | None
) -> SupportEscalationRow:
    now = datetime(2026, 12, 1, tzinfo=UTC)
    return SupportEscalationRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        account_id=None,
        platform="slack",
        platform_user_id="U1",
        channel_id="C1",
        message_id="m1",
        ma_session_id=None,
        note="",
        delivered_at=None,
        created_at=now,
        charged_to=charged_to,
        hold=hold,
        charge=charge,
        status=status,
    )


def test_received_text_when_each_kind_of_request_then_says_what_it_used() -> None:
    team = Balance(pool=D(2), pool_used=D("0.5"), cap=None, person_used=D(0))
    view = PlanView(plan=None, catalog=None, balance=team, state="active")
    settled = received_text(_request("plan", "settled", D("0.5"), D("0.5")), view)
    assert settled.endswith("\nUsed 0.5 credits. 1.5 of 2 team credits left."), settled
    pending = received_text(_request("plan", "pending", D(1), None), view)
    assert pending.endswith("\nHolding 1 credit. We will confirm the charge."), pending
    exempt = received_text(_request("exempt", "settled", D(0), D(0)), view)
    assert exempt.endswith(EXEMPT_RECEIVED), "decision 5: an admin's request used no credits"
    default = received_text(_request("default", "settled", D(1), D(1)), view)
    assert default == "Request sent. Someone will reply.", "the default plan keeps its line"


def test_settled_text_when_charged_or_declined_then_warns_the_client_is_not_told() -> None:
    assert settled_text(charge=D("0.5")) == f"Charged 0.5 credits. {CLIENT_NOT_NOTIFIED}"
    assert settled_text(charge=None) == f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}"


def test_post_status_text_when_each_state_then_reads_plainly() -> None:
    bounds = PriceBounds(D("0.5"), D(1))
    cases: tuple[tuple[RequestStatus, Decimal | None], ...] = (
        ("pending", None),
        ("settled", D(1)),
        ("declined", D(0)),
    )
    texts = [
        post_status_text(status=status, charged_to="plan", charge=charge, bounds=bounds)
        for status, charge in cases
    ]
    assert texts == ["Pending: 0.5-1 credits", "Charged 1 credit", "Declined"], texts
    exempt = post_status_text(status="settled", charged_to="exempt", charge=D(0), bounds=bounds)
    assert exempt == "No charge: an admin asked", exempt


def test_price_text_when_each_kind_then_reads_plainly() -> None:
    spec = parse_catalog(CATALOG)
    texts = [price_text(o.price) for o in spec.options]
    assert texts == ["0.5 credits", "0.5-1 credits", "1-3 credits", "Quoted"], texts
    capped = QuotedPrice(kind="quoted", max=D(2))
    assert price_text(capped) == "Quoted, up to 2 credits", "a capped quote names its max"


def test_option_text_when_each_kind_then_names_the_terms_asked_on() -> None:
    spec = parse_catalog(CATALOG)
    quick, call, review, _custom = spec.options
    texts = [
        option_text(snapshot_option(quick, step=None)),
        option_text(snapshot_option(call, step=1)),
        option_text(snapshot_option(review, step=None)),
    ]
    assert texts == [
        "Quick question: 0.5 credits",
        "Call, 1 hour: 1 credit",
        "Review: 1-3 credits",
    ], texts


def test_cheapest_bounds_when_steps_then_the_smallest_step() -> None:
    spec = parse_catalog(CATALOG)
    quick, call, *_ = spec.options
    assert cheapest_bounds(call.price) == PriceBounds(D("0.5"), D("0.5")), "smallest step"
    assert cheapest_bounds(quick.price) == price_bounds(quick.price), "others: their bounds"


def test_picked_option_when_only_text_changed_then_kept_else_none() -> None:
    spec = parse_catalog(CATALOG)
    call = spec.options[1]
    shown = option_value(call)
    assert len(shown) <= 100, "fits a Discord select value"

    def edited(**changes: object) -> SupportCatalogSpec:
        return parse_catalog({"name": "S", "options": [call.model_dump(mode="json") | changes]})

    kept = picked_option(edited(label="Phone call", description="We ring you"), shown)
    assert kept is not None and kept.label == "Phone call", "label and description are free"
    repriced = edited(price={"kind": "fixed", "amount": "1"})
    assert picked_option(repriced, shown) is None, "a new price is a new pick"
    assert picked_option(edited(fields=[]), shown) is None, "new fields are a new pick"
    assert picked_option(spec, "call") is None, "a value without its terms never matches"


def test_refusal_text_when_each_status_then_one_shared_line() -> None:
    balance = Balance(pool=None, pool_used=D(0), cap=None, person_used=D(0))
    default = PlanView(plan=None, catalog=None, balance=balance, state="active")
    exempt = PlanView(plan=None, catalog=None, balance=balance, state="active", exempt=True)
    now = datetime(2026, 12, 1, tzinfo=UTC)
    cases: tuple[tuple[SubmitStatus, PlanView, str], ...] = (
        ("duplicate", default, ALREADY_REQUESTED),
        ("out_of_credits", default, OUT_OF_CREDITS),
        ("out_of_credits", exempt, NOT_ENOUGH_CREDITS),
        ("stale", default, OPTIONS_CHANGED),
        ("plan_inactive", default, UNAVAILABLE),
        ("refused", default, UNAVAILABLE),
    )
    for status, view, want in cases:
        got = refusal_text(SubmitOutcome(status=status, row=None, view=view), now=now)
        assert got == want, (status, got)
