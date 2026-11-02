"""Pure spend-allowance rules: who an allowance covers and when it blocks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from daimon.core.allowances import (
    allowance_verdict,
    applicable,
    window_end,
    window_start,
)
from daimon.core.stores.domain import (
    AllowanceSubjectKind,
    AllowanceWindow,
    SpendAllowanceRow,
)

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
TENANT = uuid.uuid4()


def allowance(
    *,
    kind: AllowanceSubjectKind = "person",
    subject: str = "u1",
    agent: str | None = None,
    amount: str = "10",
    window: AllowanceWindow = "month",
    starts_at: datetime = NOW - timedelta(days=60),
    expires_at: datetime | None = None,
) -> SpendAllowanceRow:
    return SpendAllowanceRow(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        subject_kind=kind,
        subject_id=subject,
        agent_id=agent,
        amount_usd=Decimal(amount),
        window=window,
        starts_at=starts_at,
        expires_at=expires_at,
        created_by_account_id=None,
        created_at=starts_at,
    )


def covering(rows: list[SpendAllowanceRow], **overrides: object) -> list[SpendAllowanceRow]:
    base: dict[str, object] = {
        "person_ids": frozenset({"u1"}),
        "groups": frozenset[str](),
        "invite_id": None,
        "agent_id": "a1",
        "now": NOW,
    }
    return applicable(rows, **{**base, **overrides})  # pyright: ignore[reportArgumentType]


def test_applicable_matches_each_kind_the_person_is_covered_by() -> None:
    team = allowance(kind="group", subject="team")
    everyone = allowance(kind="group", subject="everyone")
    outside_group = allowance(kind="group", subject="staff")
    rows = [team, everyone, outside_group]
    got = covering(rows, groups=frozenset({"team"}))
    assert {r.id for r in got} == {team.id, everyone.id}, (
        "the person's own group and the built-in everyone group apply, another group does not"
    )
    mine, other = allowance(subject="u1"), allowance(subject="u2")
    assert [r.id for r in covering([mine, other])] == [mine.id], "only the person's own applies"
    invite = allowance(kind="invite", subject="inv1")
    assert [r.id for r in covering([invite], invite_id="inv1")] == [invite.id], (
        "the person's invite applies"
    )


def test_applicable_person_beats_invite_and_group_even_when_smaller() -> None:
    mine = allowance(subject="u1", amount="5")
    invite = allowance(kind="invite", subject="inv1", amount="20")
    everyone = allowance(kind="group", subject="everyone", amount="40")
    assert [r.id for r in covering([everyone, invite, mine], invite_id="inv1")] == [mine.id], (
        "a person allowance wins over a larger invite or group one"
    )
    assert [r.id for r in covering([everyone, invite])] == [everyone.id], (
        "an invite the person does not hold never applies"
    )


def test_applicable_member_ranks_invite_and_group_together() -> None:
    invite = allowance(kind="invite", subject="inv1", amount="20")
    everyone = allowance(kind="group", subject="everyone", amount="40")
    got = covering([everyone, invite], invite_id="inv1")
    assert {r.id for r in got} == {invite.id, everyone.id}, (
        "a member's invite does not shadow a group; the verdict picks the one with most left"
    )


def test_applicable_visitor_is_in_no_group() -> None:
    everyone = allowance(kind="group", subject="everyone", amount="40")
    team = allowance(kind="group", subject="team")
    invite = allowance(kind="invite", subject="inv1", amount="5")
    got = covering(
        [everyone, team, invite], groups=frozenset({"team"}), invite_id="inv1", is_visitor=True
    )
    assert [r.id for r in got] == [invite.id], "a visitor gets the invite's, never a group's"
    assert covering([everyone, team], groups=frozenset({"team"}), is_visitor=True) == [], (
        "with no person or invite allowance a visitor has none, so the gate blocks"
    )


def test_applicable_matches_a_person_allowance_naming_any_linked_id() -> None:
    on_web_id = allowance(subject="web-id")
    on_stranger = allowance(subject="u9")
    got = covering([on_web_id, on_stranger], person_ids=frozenset({"u1", "web-id"}))
    assert [r.id for r in got] == [on_web_id.id], (
        "an allowance naming any of the person's ids applies, and a stranger's does not"
    )


def test_applicable_skips_an_invite_when_the_caller_has_none() -> None:
    assert covering([allowance(kind="invite", subject="inv1")]) == [], (
        "an invite allowance never applies to someone without that invite"
    )


def test_applicable_respects_the_dates() -> None:
    future = allowance(starts_at=NOW + timedelta(days=1))
    expired = allowance(expires_at=NOW - timedelta(seconds=1))
    live = allowance(expires_at=NOW + timedelta(days=1))
    assert [r.id for r in covering([future, expired, live])] == [live.id], (
        "an allowance counts from its start up to, not including, its expiry"
    )


def test_applicable_agent_specific_beats_agent_wide_even_when_smaller() -> None:
    wide = allowance(amount="100")
    narrow = allowance(agent="a1", amount="5")
    elsewhere = allowance(agent="a2", amount="500")
    got = covering([wide, narrow, elsewhere])
    assert [r.id for r in got] == [narrow.id], (
        "an allowance for this agent governs it alone; one for another agent never applies"
    )


def test_applicable_agent_wide_covers_an_agent_without_its_own_allowance() -> None:
    wide = allowance(amount="100")
    narrow = allowance(agent="a2", amount="5")
    assert [r.id for r in covering([wide, narrow])] == [wide.id], (
        "a different agent's allowance does not hide the agent-wide one"
    )
    assert [r.id for r in covering([wide, narrow], agent_id=None)] == [wide.id], (
        "a call with no agent is governed by the agent-wide allowances only"
    )


def test_verdict_largest_room_wins_among_the_governing() -> None:
    small = allowance(amount="5")
    large = allowance(amount="20", window="total")
    verdict = allowance_verdict(
        [small, large], used={small.id: Decimal(5), large.id: Decimal("2.50")}, now=NOW
    )
    assert not verdict.blocked, "room in any governing allowance lets the person through"
    assert (verdict.left_usd, verdict.total_usd, verdict.window) == (
        Decimal("17.50"),
        Decimal(20),
        "total",
    ), "the verdict reports the allowance with the most left"
    assert verdict.resets_at is None, "a total allowance never renews"


def test_verdict_blocks_when_the_best_allowance_is_used_up() -> None:
    one = allowance(amount="5")
    two = allowance(amount="8")
    verdict = allowance_verdict([one, two], used={one.id: Decimal(5), two.id: Decimal(9)}, now=NOW)
    assert verdict.blocked, "every governing allowance used up blocks"
    assert verdict.left_usd == Decimal(0), "overspend reads as nothing left, never negative"


def test_verdict_monthly_resets_on_the_first_of_next_month() -> None:
    verdict = allowance_verdict([allowance()], used={}, now=NOW)
    assert verdict.resets_at == datetime(2026, 11, 1, tzinfo=UTC), "monthly renews next month"
    december = allowance_verdict([allowance()], used={}, now=datetime(2026, 12, 31, tzinfo=UTC))
    assert december.resets_at == datetime(2027, 1, 1, tzinfo=UTC), "December rolls into the year"


def test_verdict_without_an_allowance_is_unlimited_unless_a_visitor() -> None:
    member = allowance_verdict([], used={}, now=NOW)
    visitor = allowance_verdict([], used={}, now=NOW, is_visitor=True)
    assert (member.blocked, member.left_usd) == (False, None), "a member with none is unlimited"
    assert (visitor.blocked, visitor.left_usd) == (True, Decimal(0)), (
        "a visitor with none is blocked"
    )


def test_window_start_is_the_later_of_month_start_and_the_allowance_start() -> None:
    old = allowance(window="month")
    recent = allowance(window="month", starts_at=datetime(2026, 10, 10, tzinfo=UTC))
    total = allowance(window="total")
    assert window_start(old, now=NOW) == datetime(2026, 10, 1, tzinfo=UTC), "this month's start"
    assert window_start(recent, now=NOW) == recent.starts_at, "not before the allowance began"
    assert window_start(total, now=NOW) == total.starts_at, (
        "a total allowance counts from its start"
    )
    assert window_end(total, now=NOW) is None, "a total allowance has no renewal"
