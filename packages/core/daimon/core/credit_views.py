"""What a credits page shows: balance, spend and ledger as plain values.

Every dollar figure here is billed dollars, the raw model cost times the workspace
markup, the same basis the ledger debits and allowances count. The reads take the
markup as an argument and never read settings. A surface turns these values into its
own wording and shapes; nothing here knows about HTTP.

The pure helpers (periods, projection, ledger kinds) come first; the reads after them
are the shell, one session each, and their exceptions propagate.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from daimon.core.billing import month_start
from daimon.core.permissions import WEB_PLACE_PREFIX
from daimon.core.stores import (
    identity,
    platform_names,
    tenant_ledger,
    turn_usage,
    usage_events,
    web_chat,
    web_people,
)
from daimon.core.stores.domain import TenantLedgerRow, WebPersonRow
from daimon.core.usage_recording import SPEND_LEDGER_REASONS
from sqlalchemy.ext.asyncio import AsyncSession

Period = Literal["this", "last"]
LedgerKind = Literal["topup", "promo", "adjustment"]

_MICRO = Decimal("0.000001")
_CHAT_PLATFORMS = ("discord", "slack", "teams")
_WEB_THREAD_PREFIX = WEB_PLACE_PREFIX


def is_web_spend(platform: str, channel_id: str | None) -> bool:
    """Whether a turn's spend is a web chat's: its own platform, or a linked member's
    web chat recorded under their chat platform with the `web:` place as channel."""
    return platform == "web" or (channel_id or "").startswith(WEB_PLACE_PREFIX)


def billed(cost: float | Decimal | None, markup: Decimal) -> Decimal:
    """A raw model cost as the dollars the workspace is billed; nothing for an unknown cost."""
    if cost is None:
        return Decimal(0)
    return (Decimal(str(cost)) * markup).quantize(_MICRO)


def next_month_start(now: datetime) -> datetime:
    """The first moment of the month after the one `now` is in (UTC)."""
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(year, month, 1, tzinfo=UTC)


def period_window(period: Period, *, now: datetime) -> tuple[datetime, datetime | None]:
    """The calendar month `period` names as `(start, end)`; this month has no end yet."""
    this = month_start(now)
    if period == "this":
        return this, None
    return month_start(this - timedelta(days=1)), this


def day_bounds(now: datetime) -> list[datetime]:
    """Midnight of each day of `now`'s month, then the first moment of the next month."""
    start, end = month_start(now), next_month_start(now)
    return [start + timedelta(days=n) for n in range((end - start).days)] + [end]


def projected_month(spent: Decimal, *, now: datetime) -> Decimal:
    """Where the month's spend lands at the pace so far; the spend itself on the first day."""
    start = month_start(now)
    elapsed = now - start
    if elapsed < timedelta(days=1):
        return spent
    days = (next_month_start(now) - start).days
    return (spent * days / (Decimal(elapsed.total_seconds()) / 86_400)).quantize(_MICRO)


def ledger_kind(reason: str, *, by_person: bool = False) -> LedgerKind:
    """How the credits page files a ledger row: bought, given as a promotion, or adjusted.

    A manual credit a person made from the web is a top-up; one made by the operator's
    tooling is an adjustment.
    """
    if reason == "topup" or (reason == "manual_credit" and by_person):
        return "topup"
    if reason in ("promo_credit", "trial"):
        return "promo"
    return "adjustment"


@dataclass(frozen=True)
class AgentSpend:
    """Billed spend and turns on one agent; `agent_id` None is spend with no agent recorded."""

    agent_id: str | None
    usd: Decimal
    turns: int


@dataclass(frozen=True)
class PersonSpend:
    user_id: str
    """The platform user id the spend was recorded under."""
    usd: Decimal
    turns: int


@dataclass(frozen=True)
class ChannelSpend:
    platform: str
    channel_id: str | None
    origin: str
    usd: Decimal
    turns: int


@dataclass(frozen=True)
class TurnCharge:
    """One finished turn and what it cost."""

    id: uuid.UUID
    at: datetime
    agent_id: str | None
    platform: str
    title: str | None
    """The web chat's title or the channel's name; None when neither is known."""
    usd: Decimal


@dataclass(frozen=True)
class DailySpend:
    day: datetime
    usd: Decimal


@dataclass(frozen=True)
class MonthOverview:
    balance_usd: Decimal
    spent_usd: Decimal
    turns: int
    daily: list[DailySpend]


@dataclass(frozen=True)
class LedgerLine:
    """A change to the balance that was not model use."""

    id: uuid.UUID
    at: datetime
    kind: LedgerKind
    reason: str
    usd: Decimal
    actor_account_id: uuid.UUID | None


@dataclass(frozen=True)
class PersonLabel:
    """Who a recorded user id is, for a page that names people."""

    name: str
    visitor: bool
    person_id: uuid.UUID | None
    """The web person, when the id belongs to one (their own id, or a linked account's)."""


def _agent_spend(
    costs: dict[str | None, float], turns: dict[str | None, int], markup: Decimal
) -> list[AgentSpend]:
    rows = [
        AgentSpend(agent, billed(costs.get(agent), markup), turns.get(agent, 0))
        for agent in costs.keys() | turns.keys()
    ]
    return sorted(rows, key=lambda r: (-r.usd, r.agent_id or ""))


async def spend_by_agent_for_user(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    markup: Decimal,
    since: datetime,
    until: datetime | None = None,
) -> list[AgentSpend]:
    """One person's billed spend by agent, biggest first."""
    costs = await usage_events.costs_by_agent_for_user_since(
        session, tenant_id=tenant_id, platform_user_id=platform_user_id, since=since, until=until
    )
    turns = await usage_events.turns_by_agent_for_user_since(
        session, tenant_id=tenant_id, platform_user_id=platform_user_id, since=since, until=until
    )
    return _agent_spend(costs, turns, markup)


async def spend_by_agent_in_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    markup: Decimal,
    since: datetime,
    until: datetime | None = None,
) -> list[AgentSpend]:
    """The workspace's billed spend by agent, biggest first."""
    costs = await usage_events.costs_by_agent_in_tenant_since(
        session, tenant_id=tenant_id, since=since, until=until
    )
    turns = await usage_events.turns_by_agent_in_tenant_since(
        session, tenant_id=tenant_id, since=since, until=until
    )
    return _agent_spend(costs, turns, markup)


async def spend_by_person(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    markup: Decimal,
    since: datetime,
    until: datetime | None = None,
) -> list[PersonSpend]:
    """The workspace's billed spend by recorded user, biggest first."""
    costs = await usage_events.costs_by_user_in_tenant_since(
        session, tenant_id=tenant_id, since=since, until=until
    )
    turns = await usage_events.turns_by_user_in_tenant_since(
        session, tenant_id=tenant_id, since=since, until=until
    )
    rows = [
        PersonSpend(user, billed(costs.get(user), markup), turns.get(user, 0))
        for user in costs.keys() | turns.keys()
    ]
    return sorted(rows, key=lambda r: (-r.usd, r.user_id))


async def spend_by_channel(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    markup: Decimal,
    since: datetime,
    until: datetime | None = None,
) -> list[ChannelSpend]:
    """The workspace's billed spend by channel and turn origin, biggest first.

    A turn whose cost could not be priced counts as its known part, as the channel
    usage report does.
    """
    rows = await turn_usage.usage_by_channel(session, tenant_id=tenant_id, since=since, until=until)
    spend = [
        ChannelSpend(r.platform, r.channel_id, r.origin, billed(r.known_cost_usd, markup), r.turns)
        for r in rows
    ]
    return sorted(spend, key=lambda r: (-r.usd, r.platform, r.channel_id or ""))


async def channel_names(
    session: AsyncSession, *, tenant_id: uuid.UUID, rows: Sequence[ChannelSpend]
) -> dict[tuple[str, str], str]:
    """The stored name of each `(platform, channel_id)` among `rows` that has one."""
    names: dict[tuple[str, str], str] = {}
    for platform in {r.platform for r in rows}:
        ids = {
            r.channel_id
            for r in rows
            if r.platform == platform and r.channel_id and not is_web_spend(platform, r.channel_id)
        }
        found = await platform_names.get_channel_names(
            session, tenant_id=tenant_id, platform=platform, channel_ids=ids
        )
        names |= {(platform, channel_id): name for channel_id, name in found.items()}
    return names


async def recent_turns(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    markup: Decimal,
    since: datetime,
    limit: int,
) -> list[TurnCharge]:
    """The account's newest priced turns on any platform, with a title where one is known."""
    rows = await turn_usage.list_turn_usage(
        session,
        tenant_id=tenant_id,
        since=since,
        account_id=account_id,
        priced_only=True,
        limit=limit,
    )
    chats: dict[uuid.UUID, str] = {}
    for row in rows:
        thread = row.thread_id or ""
        if row.platform == "web" and thread.startswith(_WEB_THREAD_PREFIX):
            try:
                chats[uuid.UUID(thread.removeprefix(_WEB_THREAD_PREFIX))] = ""
            except ValueError:
                continue
    chats = await web_chat.list_titles(session, tenant_id=tenant_id, thread_ids=chats.keys())
    channels = await channel_names(
        session,
        tenant_id=tenant_id,
        rows=[
            ChannelSpend(r.platform, r.channel_id, r.origin, Decimal(0), 0)
            for r in rows
            if not is_web_spend(r.platform, r.channel_id)
        ],
    )
    charges: list[TurnCharge] = []
    for row in rows:
        title: str | None = None
        if row.platform == "web":
            thread = (row.thread_id or "").removeprefix(_WEB_THREAD_PREFIX)
            title = next((t for i, t in chats.items() if str(i) == thread), None)
        elif row.channel_id is not None and not is_web_spend(row.platform, row.channel_id):
            title = channels.get((row.platform, row.channel_id))
        charges.append(
            TurnCharge(
                id=row.id,
                at=row.started_at,
                agent_id=row.agent_id,
                platform=row.platform,
                title=title,
                usd=billed(row.cost_usd, markup),
            )
        )
    return charges


async def month_overview(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime, markup: Decimal
) -> MonthOverview:
    """The workspace's balance and this month's spend, turns and daily spend."""
    balance = await tenant_ledger.get_prepaid_balance(session, tenant_id=tenant_id)
    if balance is None:
        balance = await tenant_ledger.get_balance(session, tenant_id=tenant_id)
    since = month_start(now)
    cost = await usage_events.cost_for_tenant_since(session, tenant_id=tenant_id, since=since)
    turns = await usage_events.turn_count_for_tenant_since(
        session, tenant_id=tenant_id, since=since
    )
    bounds = day_bounds(now)
    # Ledger debits already carry the markup.
    per_day = await tenant_ledger.get_spend_by_interval(
        session, tenant_id=tenant_id, bounds=bounds, reasons=SPEND_LEDGER_REASONS
    )
    return MonthOverview(
        balance_usd=balance,
        spent_usd=billed(cost, markup),
        turns=turns,
        daily=[DailySpend(day, usd) for day, usd in zip(bounds, per_day, strict=False)],
    )


def _ledger_line(row: TenantLedgerRow) -> LedgerLine:
    return LedgerLine(
        id=row.id,
        at=row.occurred_at,
        kind=ledger_kind(row.reason, by_person=row.actor_account_id is not None),
        reason=row.reason,
        usd=row.delta_usd,
        actor_account_id=row.actor_account_id,
    )


async def ledger_history(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int
) -> list[LedgerLine]:
    """The newest changes to the balance that were not model use, newest first."""
    rows = await tenant_ledger.list_entries_excluding_reasons(
        session, tenant_id=tenant_id, reasons=SPEND_LEDGER_REASONS, limit=limit
    )
    return [_ledger_line(r) for r in rows]


async def web_people_by_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, WebPersonRow]:
    """The workspace's web people among `account_ids`, keyed by account."""
    wanted = set(account_ids)
    return {
        p.account_id: p
        for p in await web_people.list_people(session, tenant_id=tenant_id)
        if p.account_id in wanted
    }


async def label_people(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_ids: Collection[str]
) -> dict[str, PersonLabel]:
    """Name each recorded user id: a web person by their own id or a linked account, else
    the name their chat platform last gave; ids nothing names are left out."""
    people = await web_people.list_people(session, tenant_id=tenant_id)
    by_id = {str(p.id): p for p in people}
    labels: dict[str, PersonLabel] = {}
    rest: list[str] = []
    for user_id in user_ids:
        person = by_id.get(user_id)
        if person is None:
            rest.append(user_id)
        else:
            labels[user_id] = PersonLabel(person.display_name, person.kind == "visitor", person.id)
    by_account = {p.account_id: p for p in people}
    for principal in await identity.list_principals_by_external_ids(
        session, tenant_id=tenant_id, external_ids=rest
    ):
        person = by_account.get(principal.account_id)
        if person is not None and principal.external_id not in labels:
            labels[principal.external_id] = PersonLabel(
                person.display_name, person.kind == "visitor", person.id
            )
    rest = [user_id for user_id in rest if user_id not in labels]
    for platform in _CHAT_PLATFORMS:
        if not rest:
            break
        known = await platform_names.get_user_names(
            session, tenant_id=tenant_id, platform=platform, user_ids=rest
        )
        for user_id, name in known.items():
            if name.label:
                labels[user_id] = PersonLabel(name.label, False, None)
        rest = [user_id for user_id in rest if user_id not in labels]
    return labels
