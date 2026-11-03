"""Spend allowances: which apply to a person, and whether one is used up. Pure.

An allowance is a per-person limit in billed dollars (see `SpendAllowance`). The
rules, shared by every surface through `spend_gate`:

- A person is covered by allowances for them, for their invite, or for a group they are
  in (`everyone` covers all but visitors, who are in no group), inside their dates.
- The most specific kind that covers them governs: person, then invite, then group, for
  a visitor, whose invite is all they have. A member's invite and group allowances rank
  together, so a promoted visitor is not capped by a spent invite. A person's own
  allowance is never widened by a generous group's.
- Within that kind, an allowance naming the turn's agent governs that agent alone; the
  others, which name no agent, govern it only when none names the agent.
- Of the governing allowances the one with the most left wins: any room is room.
- Nothing applies means no limit, except for a visitor, who is blocked.
- A cap (`is_cap`) is a monthly ceiling, never one of the allowances to choose among, so
  a generous allowance cannot step past it. The governing cap is the person's own if one
  names any of their ids (the lowest, if several), else the `everyone` cap, which covers
  visitors too. A person is blocked when the governing allowance or the cap has no room;
  what is left is the smaller of the two, and the reset follows the one that binds.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from daimon.core.stores.domain import AllowanceWindow, SpendAllowanceRow

EVERYONE = "everyone"
"""The built-in group every person belongs to."""


@dataclass(frozen=True)
class AllowanceVerdict:
    """Where a person stands against the allowance that governs them."""

    blocked: bool
    left_usd: Decimal | None
    """None when no allowance applies and the person is not blocked."""
    total_usd: Decimal | None
    window: AllowanceWindow | None
    resets_at: datetime | None
    """When a monthly allowance renews; None for a total one."""
    capped: bool = False
    """True when the cap, not an allowance, is what has no room."""


def applicable(
    allowances: Sequence[SpendAllowanceRow],
    *,
    person_ids: Collection[str],
    groups: frozenset[str],
    invite_id: str | None,
    agent_id: str | None,
    now: datetime,
    is_visitor: bool = False,
) -> list[SpendAllowanceRow]:
    """The allowances that govern a person's spend on `agent_id` at `now`.

    `person_ids` is every id the person spends under; a person allowance naming any applies.
    A visitor is in no group, `everyone` included.
    """
    member_of = frozenset[str]() if is_visitor else groups | {EVERYONE}
    live = [
        row
        for row in allowances
        if not row.is_cap
        and row.starts_at <= now
        and (row.expires_at is None or now < row.expires_at)
        and (row.agent_id is None or row.agent_id == agent_id)
    ]
    own = [r for r in live if r.subject_kind == "person" and r.subject_id in person_ids]
    invited = [
        r
        for r in live
        if r.subject_kind == "invite" and invite_id is not None and r.subject_id == invite_id
    ]
    grouped = [r for r in live if r.subject_kind == "group" and r.subject_id in member_of]
    by_kind = (own, invited) if is_visitor else (own, invited + grouped)
    covering = next((rows for rows in by_kind if rows), None) or []
    for_agent = [row for row in covering if row.agent_id is not None]
    return for_agent or covering


def governing_cap(
    allowances: Sequence[SpendAllowanceRow], *, person_ids: Collection[str], now: datetime
) -> SpendAllowanceRow | None:
    """The cap that holds a person at `now`: their own, else the `everyone` cap.

    Visitors are held by the `everyone` cap like anyone else. Of several caps of the same
    kind the lowest holds.
    """
    live = [
        row
        for row in allowances
        if row.is_cap and row.starts_at <= now and (row.expires_at is None or now < row.expires_at)
    ]
    own = [r for r in live if r.subject_kind == "person" and r.subject_id in person_ids]
    everyone = [r for r in live if r.subject_kind == "group" and r.subject_id == EVERYONE]
    held = own or everyone
    return min(held, key=lambda r: (r.amount_usd, r.created_at, r.id), default=None)


def window_start(allowance: SpendAllowanceRow, *, now: datetime) -> datetime:
    """The start of the spend an allowance counts: its own start, or this month's if later.

    A cap always counts the whole month, whenever its row was set.
    """
    if allowance.window == "total":
        return allowance.starts_at
    month = datetime(now.year, now.month, 1, tzinfo=UTC)
    if allowance.is_cap:
        return month
    return max(allowance.starts_at, month)


def window_end(allowance: SpendAllowanceRow, *, now: datetime) -> datetime | None:
    """When a monthly allowance renews (the next month's first day); None for a total one."""
    if allowance.window == "total":
        return None
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=UTC)


def allowance_verdict(
    governing: Sequence[SpendAllowanceRow],
    *,
    used: Mapping[uuid.UUID, Decimal],
    now: datetime,
    is_visitor: bool = False,
    cap: SpendAllowanceRow | None = None,
) -> AllowanceVerdict:
    """Whether the person is blocked, given what each governing allowance and the cap used.

    `used` maps an allowance id to the billed dollars counted inside its window; a
    missing id has used nothing. No governing allowance blocks a visitor only. The cap
    holds on top: with no room under it the person is blocked whatever the allowance says.
    """
    verdict = _allowance_part(governing, used=used, now=now, is_visitor=is_visitor)
    if cap is None:
        return verdict
    cap_left = max(cap.amount_usd - used.get(cap.id, Decimal(0)), Decimal(0))
    if verdict.left_usd is not None and verdict.left_usd < cap_left:
        return verdict
    return AllowanceVerdict(
        blocked=verdict.blocked or cap_left <= 0,
        left_usd=cap_left,
        total_usd=cap.amount_usd,
        window=cap.window,
        resets_at=window_end(cap, now=now),
        capped=cap_left <= 0,
    )


def _allowance_part(
    governing: Sequence[SpendAllowanceRow],
    *,
    used: Mapping[uuid.UUID, Decimal],
    now: datetime,
    is_visitor: bool,
) -> AllowanceVerdict:
    if not governing:
        return AllowanceVerdict(
            blocked=is_visitor,
            left_usd=Decimal(0) if is_visitor else None,
            total_usd=Decimal(0) if is_visitor else None,
            window=None,
            resets_at=None,
        )
    best = max(
        governing,
        key=lambda row: (row.amount_usd - used.get(row.id, Decimal(0)), row.amount_usd),
    )
    left = max(best.amount_usd - used.get(best.id, Decimal(0)), Decimal(0))
    return AllowanceVerdict(
        blocked=left <= 0,
        left_usd=left,
        total_usd=best.amount_usd,
        window=best.window,
        resets_at=window_end(best, now=now),
    )
