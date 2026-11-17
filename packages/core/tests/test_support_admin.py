"""The admin read of support limits and the settle wording every admin surface shares."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelActor, Expected
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.support_rows import SupportLimitRow
from daimon.core.support import (
    CLIENT_NOT_NOTIFIED,
    RequestChoice,
    SupportError,
    SupportPlace,
    parse_limit_spec,
    parse_menu,
)
from daimon.core.support_admin import (
    NO_REQUEST,
    LimitChange,
    get_limit_usage,
    list_limit_usage,
    merge_limit,
    merge_limit_change,
    settle_outcome_text,
    usage_text,
)
from daimon.core.support_changes import set_limit, set_menu, set_menu_use, settle
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

D = Decimal
NOW = datetime(2026, 11, 17, tzinfo=UTC)
MENU = parse_menu(
    {
        "name": "Support",
        "options": [
            {"key": "review", "label": "Review", "price": {"kind": "range", "min": "1", "max": "3"}}
        ],
    }
)

type Factory = async_sessionmaker[AsyncSession]


def _actor(tenant_id: uuid.UUID) -> ChannelActor:
    return ChannelActor(
        tenant_id=tenant_id, account_id=None, subject=Subject(is_admin=True), surface="cli"
    )


async def _setup(db_session: AsyncSession, factory: Factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Everywhere: 2 each in October (ended); C1: 5 shared, U1 capped at 4; one pending request."""
    tenant = await make_tenant(db_session, platform="slack")
    await db_session.commit()
    actor = _actor(tenant.id)
    await set_menu(factory, actor, platform="slack", key="support", spec=MENU)
    await set_menu_use(
        factory, actor, platform="slack", scope="workspace", scope_id="", key="support"
    )
    limits = [
        parse_limit_spec(scope="channel", scope_id="C1", shared="5"),
        parse_limit_spec(
            scope="workspace", each_person="2", starts_at="2026-10-01", ends_at="2026-11-01"
        ),
        parse_limit_spec(scope="channel", scope_id="C1", platform_user_id="U1", each_person="4"),
    ]
    for spec in limits:
        await set_limit(factory, actor, platform="slack", spec=spec)
    async with factory.begin() as session:
        outcome = await ledger.submit_request(
            session,
            tenant_id=tenant.id,
            account_id=None,
            platform="slack",
            platform_user_id="U1",
            channel_id="C1",
            message_id="m1",
            ma_session_id=None,
            place=SupportPlace(channel_id="C1", agent_id=None),
            choice=RequestChoice.of(MENU.options[0]),
            default_allowance=20,
        )
    assert outcome.row is not None, "a pending request"
    return tenant.id, outcome.row.id


async def test_list_limit_usage_when_limits_exist_then_workspace_first_with_shared_used(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id, _ = await _setup(db_session, db_session_factory)
    usage = await list_limit_usage(db_session, tenant_id=tenant_id, now=NOW)
    assert [u.limit.scope for u in usage] == ["workspace", "channel"], (
        "places only, workspace first"
    )
    workspace, channel = usage
    assert workspace.state == "ended", "its dates ended on 1 Nov"
    assert workspace.summary == "Ended: 2 each, from 1 Oct 00:00 UTC to 1 Nov 00:00 UTC", (
        "an ended limit says so, with the exact dates an admin set"
    )
    assert (workspace.menu, channel.menu) == ("support", None), "the menu a place picked itself"
    assert channel.shared_used == D(1), "the pending range request holds its minimum"
    assert channel.summary == "1 of 5 credits used, all time", "shared credits and dates in words"
    assert [p.platform_user_id for p in channel.people] == ["U1"], "own limits listed"
    assert workspace.people == (), "U1's row is in C1, not everywhere"


async def test_get_limit_usage_when_missing_then_none(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id, _ = await _setup(db_session, db_session_factory)
    found = await get_limit_usage(
        db_session, tenant_id=tenant_id, scope="channel", scope_id="C1", now=NOW
    )
    missing = await get_limit_usage(
        db_session, tenant_id=tenant_id, scope="agent", scope_id="agent_x", now=NOW
    )
    assert found is not None and found.limit.shared == D(5), "the channel limit"
    assert missing is None, "no agent limit"


async def test_merge_limit_change_when_a_person_row_then_reads_that_row_and_its_revision(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id, _ = await _setup(db_session, db_session_factory)
    spec, revision = await merge_limit_change(
        db_session,
        tenant_id=tenant_id,
        scope="channel",
        scope_id="C1",
        change=LimitChange(ends_at="2027-01-01"),
        platform_user_id="U1",
    )
    assert (spec.platform_user_id, spec.each_person, spec.shared) == ("U1", D(4), None), "kept"
    assert spec.ends_at == datetime(2027, 1, 1, tzinfo=UTC) and revision is not None


async def test_settle_outcome_text_when_each_outcome_then_worded_for_the_settler(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    tenant_id, request_id = await _setup(db_session, db_session_factory)
    actor = _actor(tenant_id)
    over = await settle(
        db_session_factory, actor, escalation_id=request_id, charge=D(9), default_allowance=20
    )
    assert settle_outcome_text(over) == "Enter an amount from 1 to 3.", "the range"
    done = await settle(
        db_session_factory, actor, escalation_id=request_id, charge=D(2), default_allowance=20
    )
    assert settle_outcome_text(done) == f"Charged 2 credits. {CLIENT_NOT_NOTIFIED}", "charged"
    again = await settle(
        db_session_factory, actor, escalation_id=request_id, charge=D(3), default_allowance=20
    )
    assert settle_outcome_text(again) == "Already charged 2 credits.", "already settled"
    fixed = await settle(
        db_session_factory,
        actor,
        escalation_id=request_id,
        charge=None,
        default_allowance=20,
        expected=Expected(D(2)),
    )
    assert settle_outcome_text(fixed).startswith("Declined."), "a correction to a decline"
    missing = await settle(
        db_session_factory, actor, escalation_id=uuid.uuid4(), charge=D(1), default_allowance=20
    )
    assert settle_outcome_text(missing) == NO_REQUEST, "no such request"


def _row(**fields: object) -> SupportLimitRow:
    row: dict[str, object] = {
        "id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "scope": "channel",
        "scope_id": "C1",
        "platform_user_id": None,
        "shared": D(10),
        "each_person": D(2),
        "window": "total",
        "starts_at": datetime(2026, 11, 18, 9, tzinfo=UTC),
        "ends_at": datetime(2026, 11, 24, 9, tzinfo=UTC),
        "revision": 1,
        "set_by_account_id": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return SupportLimitRow.model_validate(row | fields)


def test_usage_text_when_dates_span_years_then_exact_times_in_utc() -> None:
    limit = _row(
        each_person=None,
        starts_at=datetime(2026, 11, 24, 9, tzinfo=UTC),
        ends_at=datetime(2027, 1, 1, tzinfo=UTC),
    )
    text = usage_text(limit, shared_used=D(1), now=NOW)
    assert text == (
        "Not started: 1 of 10 credits used, from 24 Nov 09:00 UTC to 1 Jan 2027 00:00 UTC"
    ), text
    monthly = usage_text(
        _row(window="monthly", starts_at=None, ends_at=None), shared_used=D(0), now=NOW
    )
    assert monthly == "0 of 10 credits used, 2 each, each month", monthly


DATED = _row()


def test_merge_limit_when_options_left_out_then_keeps_amounts_and_dates() -> None:
    spec = merge_limit(DATED, scope="channel", scope_id="C1", change=LimitChange(shared="12"))
    assert (spec.shared, spec.each_person, spec.window) == (D(12), D(2), "total"), "kept"
    assert (spec.starts_at, spec.ends_at) == (DATED.starts_at, DATED.ends_at), "dates kept"
    new = merge_limit(None, scope="channel", scope_id="C1", change=LimitChange(shared="1"))
    assert (new.each_person, new.window, new.ends_at) == (None, "total", None), (
        "with no limit, what is left out is unset"
    )


def test_merge_limit_when_none_or_a_new_window_then_clears_only_what_it_names() -> None:
    cleared = merge_limit(
        DATED,
        scope="channel",
        scope_id="C1",
        change=LimitChange(each_person="none", ends_at="none"),
    )
    assert (cleared.shared, cleared.each_person, cleared.ends_at) == (D(10), None, None), "cleared"
    monthly = merge_limit(
        DATED, scope="channel", scope_id="C1", change=LimitChange(window="monthly")
    )
    assert (monthly.window, monthly.starts_at, monthly.ends_at) == (
        "monthly",
        DATED.starts_at,
        DATED.ends_at,
    ), "dates bind a monthly limit too"
    with pytest.raises(SupportError, match="Set a shared limit"):
        merge_limit(
            DATED,
            scope="channel",
            scope_id="C1",
            change=LimitChange(shared="none", each_person="none"),
        )
    with pytest.raises(SupportError, match="end must be after"):
        merge_limit(DATED, scope="channel", scope_id="C1", change=LimitChange(ends_at="2026-11-01"))


def test_merge_limit_when_a_person_row_then_shared_credits_are_refused() -> None:
    person = _row(platform_user_id="U1", shared=None, starts_at=None, ends_at=None)
    kept = merge_limit(
        person, scope="channel", scope_id="C1", change=LimitChange(), platform_user_id="U1"
    )
    assert (kept.platform_user_id, kept.each_person) == ("U1", D(2)), "their own cap"
    with pytest.raises(SupportError, match="no shared credits"):
        merge_limit(
            person,
            scope="channel",
            scope_id="C1",
            change=LimitChange(shared="5"),
            platform_user_id="U1",
        )
