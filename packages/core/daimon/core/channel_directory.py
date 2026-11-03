"""Every channel of a workspace, one row each, with where each value comes from.

There is no channels table: a channel is listed when a setting, a budget, the
access policy, a known name or spend in the last 30 days names it. The private
channels DM conversations run in are left out. Web chat is one row,
`WEB_ROW_ID`, gathering every person's web spend. Spend is read once for all
rows: usage rows priced like the Credits page (`channel_cost_totals`) for what a channel
spent, and the ledger (`list_window_spend`) for a budget, which is what the budget gate counts.

`build_channel_rows` is pure; `load_channel_directory` and `load_channel_row`
are its shell.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from daimon.core.access_policy import ChannelReaders, ChannelWriters, TenantAccessPolicy
from daimon.core.answering_map import AnsweringMap, load_answering_map
from daimon.core.billing import month_start
from daimon.core.channel_budget import ChannelBudgetStatus, budget_period, is_budget_active
from daimon.core.credit_views import billed, recent_start
from daimon.core.errors import StoreError
from daimon.core.permissions import (
    WEB_PLACE_ID,
    WEB_PLACE_PREFIX,
    channel_rule,
    runs_only_in,
)
from daimon.core.scope import ConfigTier, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_budgets import list_channel_budgets
from daimon.core.stores.channel_directory import (
    list_configured_channel_ids,
    list_shared_channel_ids,
)
from daimon.core.stores.direct_messages import list_dm_channel_ids
from daimon.core.stores.domain import ChannelBudgetRow, ChannelNameRow
from daimon.core.stores.platform_names import list_channel_names
from daimon.core.stores.tenant_ledger import list_window_spend
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.usage_events import ChannelCostTotals, channel_cost_totals
from sqlalchemy.ext.asyncio import AsyncSession

WEB_ROW_ID = WEB_PLACE_ID
"""The one row web chat gets."""

ValueSource = Literal["channel", "workspace", "deployment"]
"""Where a cascaded value comes from: the channel's own, the workspace default, or the
deployment's."""
AccessTag = Literal["kept_inside", "locked", "shared"]
"""`kept_inside`: only this channel reads it. `locked`: only its own agents read it
(a confidential channel). `shared`: shared with another organisation."""

_SOURCES: Mapping[ConfigTier, ValueSource] = {
    "thread": "channel",
    "channel": "channel",
    "tenant": "workspace",
    "deployment": "deployment",
}


@dataclass(frozen=True)
class Sourced:
    """A cascaded value and where it comes from; both None when nothing sets it."""

    value: str | None
    source: ValueSource | None

    @classmethod
    def of(cls, found: tuple[str | None, ConfigTier | None]) -> Sourced:
        value, tier = found
        return cls(value, _SOURCES[tier] if tier is not None else None)


@dataclass(frozen=True)
class ChannelSpend:
    month_usd: Decimal = Decimal(0)
    """Since the start of this UTC month."""
    recent_usd: Decimal = Decimal(0)
    """In the 30 days Credits counts as recent, today included."""
    last_active_at: datetime | None = None
    """The latest spend."""


@dataclass(frozen=True)
class ChannelRow:
    channel_id: str
    platform: str
    """The workspace's platform, or "web" for `WEB_ROW_ID`."""
    name: ChannelNameRow | None
    agent: Sourced
    environment: Sourced
    readers: ChannelReaders
    writers: ChannelWriters
    kept_agents: tuple[str, ...]
    tags: tuple[AccessTag, ...]
    budget: ChannelBudgetStatus | None
    spend: ChannelSpend

    @property
    def confidential(self) -> bool:
        return self.readers == "own"


@dataclass(frozen=True)
class ChannelDirectory:
    platform: str
    default_agent: Sourced
    default_environment: Sourced
    rows: tuple[ChannelRow, ...]


def policy_channel_ids(policy: TenantAccessPolicy) -> set[str]:
    """Channels the access policy names: a channel rule, or an agent kept to channels.

    A Slack thread's rule (`channel:ts`) and the web chat place are not channels.
    """
    ruled = set(policy.channel_rules)
    for rule in policy.agent_rules.values():
        ruled.update(rule.runs_in or ())
    return {channel_id for channel_id in ruled if ":" not in channel_id} - {WEB_PLACE_ID}


def is_web_channel(channel_id: str) -> bool:
    return channel_id == WEB_PLACE_ID or channel_id.startswith(WEB_PLACE_PREFIX)


def fold_web_spend(
    totals: Mapping[str, ChannelCostTotals], markup: Decimal
) -> dict[str, ChannelSpend]:
    """Billed spend by row: every person's web chat adds up under `WEB_ROW_ID`. Pure."""
    spend: dict[str, ChannelSpend] = {}
    for channel_id, total in totals.items():
        key = WEB_ROW_ID if is_web_channel(channel_id) else channel_id
        prior = spend.get(key, ChannelSpend())
        latest = max(
            (at for at in (prior.last_active_at, total.last_at) if at is not None),
            default=None,
        )
        spend[key] = ChannelSpend(
            prior.month_usd + billed(total.month_cost, markup),
            prior.recent_usd + billed(total.days_cost, markup),
            latest,
        )
    return spend


def _tags(policy: TenantAccessPolicy, channel_id: str, shared: bool) -> tuple[AccessTag, ...]:
    readers = channel_rule(policy, channel_id).readers
    tags: list[AccessTag] = []
    if readers == "inside":
        tags.append("kept_inside")
    if readers == "own":
        tags.append("locked")
    if shared:
        tags.append("shared")
    return tuple(tags)


def build_channel_rows(
    channel_ids: Collection[str],
    *,
    platform: str,
    policy: TenantAccessPolicy,
    answering: AnsweringMap,
    names: Mapping[str, ChannelNameRow],
    shared: Collection[str],
    budgets: Mapping[str, ChannelBudgetStatus],
    spend: Mapping[str, ChannelSpend],
) -> tuple[ChannelRow, ...]:
    """One row per id, web chat first, then by name and id. Pure."""
    rows: list[ChannelRow] = []
    for channel_id in channel_ids:
        web = channel_id == WEB_ROW_ID
        rule = channel_rule(policy, channel_id)
        rows.append(
            ChannelRow(
                channel_id=channel_id,
                platform="web" if web else platform,
                name=names.get(channel_id),
                agent=Sourced.of(answering.agent_in(None if web else channel_id)),
                environment=Sourced.of(answering.environment_in(None if web else channel_id)),
                readers=rule.readers,
                writers=rule.writers,
                kept_agents=() if web else runs_only_in(policy, channel_id),
                tags=() if web else _tags(policy, channel_id, channel_id in shared),
                budget=budgets.get(channel_id),
                spend=spend.get(channel_id, ChannelSpend()),
            )
        )

    def order(row: ChannelRow) -> tuple[bool, str, str]:
        label = row.name.name.casefold() if row.name is not None else "￿"
        return (row.channel_id != WEB_ROW_ID, label, row.channel_id)

    return tuple(sorted(rows, key=order))


async def _budget_statuses(
    session: AsyncSession, budgets: list[ChannelBudgetRow], *, tenant_id: uuid.UUID, now: datetime
) -> dict[str, ChannelBudgetStatus]:
    periods = {budget.channel_id: budget_period(budget, now=now) for budget in budgets}
    spent = await list_window_spend(
        session,
        tenant_id=tenant_id,
        windows=[(cid, period.since, period.until) for cid, period in periods.items()],
    )
    return {
        budget.channel_id: ChannelBudgetStatus(
            budget=budget,
            spent_usd=spent.get(budget.channel_id, Decimal(0)),
            is_active=is_budget_active(budget, now=now),
        )
        for budget in budgets
    }


@dataclass(frozen=True)
class _Reads:
    platform: str
    policy: TenantAccessPolicy
    answering: AnsweringMap
    names: dict[str, ChannelNameRow]
    shared: set[str]
    budgets: dict[str, ChannelBudgetStatus]
    spend: dict[str, ChannelSpend]
    dms: set[str]


async def _read(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
    now: datetime,
    markup: Decimal,
) -> _Reads:
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        raise StoreError(f"no tenant {tenant_id}")
    platform = tenant.platform
    budgets = [
        row
        for row in await list_channel_budgets(session, tenant_id=tenant_id)
        if row.platform == platform
    ]
    totals = await channel_cost_totals(
        session,
        tenant_id=tenant_id,
        month_start=month_start(now),
        days_start=recent_start(now),
    )
    return _Reads(
        platform=platform,
        policy=await load_access_policy(session, tenant_id=tenant_id),
        answering=await load_answering_map(
            session, tenant_id=tenant_id, platform=platform, default=default
        ),
        names={
            row.channel_id: row
            for row in await list_channel_names(session, tenant_id=tenant_id, platform=platform)
        },
        shared=await list_shared_channel_ids(session, tenant_id=tenant_id, platform=platform),
        budgets=await _budget_statuses(session, budgets, tenant_id=tenant_id, now=now),
        spend=fold_web_spend(totals, markup),
        dms=await list_dm_channel_ids(session, tenant_id=tenant_id),
    )


def _rows(reads: _Reads, channel_ids: Collection[str]) -> tuple[ChannelRow, ...]:
    return build_channel_rows(
        channel_ids,
        platform=reads.platform,
        policy=reads.policy,
        answering=reads.answering,
        names=reads.names,
        shared=reads.shared,
        budgets=reads.budgets,
        spend=reads.spend,
    )


async def load_channel_directory(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
    now: datetime,
    markup: Decimal,
) -> ChannelDirectory:
    """Every channel of the workspace; raises `AccessPolicyUnreadable` like the policy read.

    Spend is billed dollars: usage priced and multiplied by `markup`.
    """
    reads = await _read(session, tenant_id=tenant_id, default=default, now=now, markup=markup)
    used = {cid for cid, spend in reads.spend.items() if spend.recent_usd > 0}
    listed = (
        await list_configured_channel_ids(session, tenant_id=tenant_id, platform=reads.platform)
        | policy_channel_ids(reads.policy)
        | reads.budgets.keys()
        | used
    )
    listed = {cid for cid in listed if not is_web_channel(cid)} - reads.dms
    return ChannelDirectory(
        platform=reads.platform,
        default_agent=Sourced.of(reads.answering.agent_in(None)),
        default_environment=Sourced.of(reads.answering.environment_in(None)),
        rows=_rows(reads, {*listed, WEB_ROW_ID}),
    )


async def load_channel_row(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    default: DeploymentDefault,
    now: datetime,
    markup: Decimal,
) -> ChannelRow | None:
    """One channel's row, listed or not; None for a DM's channel."""
    reads = await _read(session, tenant_id=tenant_id, default=default, now=now, markup=markup)
    if channel_id in reads.dms or (channel_id != WEB_ROW_ID and is_web_channel(channel_id)):
        return None
    return _rows(reads, (channel_id,))[0]
