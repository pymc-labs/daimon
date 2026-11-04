"""The credit pages' figures as plain data: a person's own use, the workspace overview and
spend by person, agent or channel. Shared by the web app and the chat tools.

Every figure is billed dollars (model cost times the workspace markup). Names of agents and
channels come in through lookups, so each surface asks its own platform.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, NamedTuple

from daimon.core import credit_views
from daimon.core.platform_names import UNNAMED_CHANNEL
from daimon.core.stores import tenants as tenants_store
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NO_AGENT: Final = "other"
NO_PERSON: Final = "none"
NO_CHANNEL: Final = "none"
LEDGER_LIMIT: Final = 50
RECENT_TURNS: Final = 12
_CENT: Final = Decimal("0.01")

SpendBy = Literal["person", "agent", "channel"]


def usd_of(value: Decimal) -> float:
    return float(value.quantize(_CENT))


class AgentLabel(NamedTuple):
    name: str
    avatar: str = ""


AgentLabels = Callable[[Collection[str]], Awaitable[Mapping[str, AgentLabel]]]
ChannelNames = Callable[
    [dict[tuple[str, str], str], Sequence[tuple[str, str]]], Awaitable[dict[tuple[str, str], str]]
]


@dataclass(frozen=True)
class SpendEntry:
    id: str
    name: str
    turns: int
    usd: float
    avatar: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class TurnEntry:
    id: str
    at: str
    agent_id: str | None
    title: str
    platform: str
    usd: float


@dataclass(frozen=True)
class MineReport:
    by_agent: list[SpendEntry]
    turns: list[TurnEntry]
    spent_usd: float
    turn_count: int


@dataclass(frozen=True)
class LedgerEntry:
    id: str
    at: str
    kind: Literal["topup", "promo", "adjustment"]
    amount_usd: float
    by: str
    note: str


@dataclass(frozen=True)
class DailyEntry:
    day: str
    usd: float


@dataclass(frozen=True)
class OverviewReport:
    balance_usd: float
    spent_usd: float
    """The last 30 days."""
    turns: int
    spent_month_usd: float
    projected_month_usd: float
    daily: list[DailyEntry]
    ledger: list[LedgerEntry]
    funding: Literal["prepaid", "operator_funded"]


def merged(rows: Sequence[SpendEntry]) -> list[SpendEntry]:
    """One row per id, summed, biggest first."""
    out: dict[str, SpendEntry] = {}
    for row in rows:
        seen = out.get(row.id)
        out[row.id] = (
            row
            if seen is None
            else SpendEntry(
                id=row.id,
                name=row.name,
                turns=seen.turns + row.turns,
                usd=seen.usd + row.usd,
                avatar=row.avatar,
                kind=row.kind,
            )
        )
    return sorted(out.values(), key=lambda r: (-r.usd, r.name))


def agent_entries(
    spend: Sequence[credit_views.AgentSpend], labels: Mapping[str, AgentLabel]
) -> list[SpendEntry]:
    return merged(
        [
            SpendEntry(
                id=s.agent_id or NO_AGENT,
                name=labels[s.agent_id].name if s.agent_id else "Other",
                avatar=labels[s.agent_id].avatar if s.agent_id else "",
                turns=s.turns,
                usd=usd_of(s.usd),
            )
            for s in spend
        ]
    )


def channel_entry(
    spend: credit_views.ChannelSpend, names: Mapping[tuple[str, str], str]
) -> SpendEntry:
    if spend.origin == "routine":
        row_id, name, kind = "routines", "Routines", "routine"
    elif credit_views.is_web_spend(spend.platform, spend.channel_id):
        row_id, name, kind = "web", "Web", "web"
    elif spend.channel_id is None:
        row_id, name, kind = NO_CHANNEL, "No channel", None
    else:
        row_id = f"{spend.platform}:{spend.channel_id}"
        name = names.get((spend.platform, spend.channel_id or ""), UNNAMED_CHANNEL)
        kind = spend.platform
    return SpendEntry(id=row_id, name=name, kind=kind, turns=spend.turns, usd=usd_of(spend.usd))


def person_entries(
    people: Sequence[credit_views.PersonSpend], labels: Mapping[str, credit_views.PersonLabel]
) -> list[SpendEntry]:
    rows: list[SpendEntry] = []
    for person in people:
        if person.user_id is None:
            rows.append(
                SpendEntry(
                    id=NO_PERSON, name="No person", turns=person.turns, usd=usd_of(person.usd)
                )
            )
            continue
        label = labels.get(person.user_id)
        rows.append(
            SpendEntry(
                id=str(label.person_id) if label and label.person_id else person.user_id,
                name=label.name if label else "Unknown person",
                kind="Visitor" if label and label.visitor else None,
                turns=person.turns,
                usd=usd_of(person.usd),
            )
        )
    return merged(rows)


def ledger_wording(line: credit_views.LedgerLine, actor: str | None) -> tuple[str, str]:
    """Who and what a ledger row reads as."""
    if line.actor_account_id is not None:
        return actor or "An admin", "Manual grant"
    named = {
        "topup": ("Stripe", "Card payment"),
        "promo_credit": ("Daimon", "Promo code"),
        "trial": ("Daimon", "Welcome credit"),
        "manual_credit": ("Support", "Manual credit"),
    }
    return named.get(line.reason, ("Daimon", line.reason.replace("_", " ").capitalize()))


async def my_credits(
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform_user_id: str,
    period: credit_views.Period,
    now: datetime,
    markup: Decimal,
    agent_labels: AgentLabels,
) -> MineReport:
    """One person's use by agent over `period`, and their latest turns."""
    since, until = credit_views.period_window(period, now=now)
    async with sessions() as db:
        spend = await credit_views.spend_by_agent_for_user(
            db,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            markup=markup,
            since=since,
            until=until,
        )
        charges = await credit_views.recent_turns(
            db,
            tenant_id=tenant_id,
            account_id=account_id,
            markup=markup,
            since=now - timedelta(days=30),
            limit=RECENT_TURNS,
        )
    by_agent = agent_entries(spend, await agent_labels({s.agent_id for s in spend if s.agent_id}))
    return MineReport(
        by_agent=by_agent,
        turns=[
            TurnEntry(
                id=str(c.id),
                at=c.at.isoformat(),
                agent_id=c.agent_id or None,
                title=c.title or "Conversation",
                platform=c.platform,
                usd=usd_of(c.usd),
            )
            for c in charges
        ],
        spent_usd=usd_of(sum((s.usd for s in spend), Decimal(0))),
        turn_count=sum(r.turns for r in by_agent),
    )


async def overview_report(
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    now: datetime,
    markup: Decimal,
) -> OverviewReport:
    """The workspace's balance, recent spend, projection and credit history."""
    async with sessions() as db:
        recent = await credit_views.spend_overview(db, tenant_id=tenant_id, now=now, markup=markup)
        lines = await credit_views.ledger_history(db, tenant_id=tenant_id, limit=LEDGER_LIMIT)
        actors = await credit_views.web_people_by_account(
            db,
            tenant_id=tenant_id,
            account_ids={x.actor_account_id for x in lines if x.actor_account_id},
        )
        tenant = await tenants_store.get_tenant(db, tenant_id)
    ledger: list[LedgerEntry] = []
    for line in lines:
        actor = actors.get(line.actor_account_id) if line.actor_account_id else None
        by, note = ledger_wording(line, actor.display_name if actor else None)
        ledger.append(
            LedgerEntry(
                id=str(line.id),
                at=line.at.isoformat(),
                kind=line.kind,
                amount_usd=usd_of(line.usd),
                by=by,
                note=note,
            )
        )
    return OverviewReport(
        balance_usd=usd_of(recent.balance_usd),
        spent_usd=usd_of(recent.spent_usd),
        turns=recent.turns,
        spent_month_usd=usd_of(recent.month_usd),
        projected_month_usd=usd_of(credit_views.projected_month(recent.month_usd, now=now)),
        daily=[DailyEntry(day=d.day.isoformat(), usd=usd_of(d.usd)) for d in recent.daily],
        ledger=ledger,
        funding=tenant.funding_mode if tenant else "prepaid",
    )


async def spend_breakdown(
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    by: SpendBy,
    period: credit_views.Period,
    now: datetime,
    markup: Decimal,
    agent_labels: AgentLabels,
    channel_names: ChannelNames,
) -> list[SpendEntry]:
    """The workspace's spend over `period`, one row per person, agent or channel."""
    since, until = credit_views.period_window(period, now=now)
    if by == "channel":
        async with sessions() as db:
            channels = await credit_views.spend_by_channel(
                db, tenant_id=tenant_id, markup=markup, since=since, until=until
            )
            names = await credit_views.channel_names(db, tenant_id=tenant_id, rows=channels)
        # Asked outside the read: a platform answer can take a few seconds.
        names = await channel_names(
            names, [(c.platform, c.channel_id) for c in channels if c.channel_id]
        )
        return merged([channel_entry(c, names) for c in channels])
    async with sessions() as db:
        if by == "agent":
            by_agent = await credit_views.spend_by_agent_in_tenant(
                db, tenant_id=tenant_id, markup=markup, since=since, until=until
            )
        else:
            people = await credit_views.spend_by_person(
                db, tenant_id=tenant_id, markup=markup, since=since, until=until
            )
            labels = await credit_views.label_people(
                db, tenant_id=tenant_id, user_ids=[p.user_id for p in people if p.user_id]
            )
            return person_entries(people, labels)
    return agent_entries(by_agent, await agent_labels({s.agent_id for s in by_agent if s.agent_id}))
