"""Tenant summary: the balance, funding mode and every configured channel in one read.

A channel is listed when it has its own agent or environment setting, a
budget, channel admins or isolation. The private channels DM conversations
run in are left out. The MCP `get_tenant_summary` tool and `daimon channels
list` both read it, as a server admin sees each channel
(`daimon.core.channel_overview`): its `sealed` and `protected` flags are
filled only for a caller that can already read the access policy (the CLI);
for everyone else they are omitted, never sent as false.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_budget import ChannelBudgetStatus, load_budget_status
from daimon.core.channel_overview import ChannelAdminSet, ChannelViewer, build_channel_overview
from daimon.core.errors import StoreError
from daimon.core.permissions import confidential_channels
from daimon.core.promo_credit import get_active_timed_credit
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow, merge
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.channel_budgets import list_channel_budgets
from daimon.core.stores.direct_messages import list_dm_channel_ids
from daimon.core.stores.domain import ChannelAdminsRow, FundingMode
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.tenant_ledger import get_balance
from daimon.core.stores.tenants import get_tenant
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass
from sqlalchemy.ext.asyncio import AsyncSession


def _omitted(value: object) -> bool:
    return value is None


@dataclass(frozen=True)
class ChannelAdmins:
    role_ids: list[str]
    """Group ids: Discord roles, Slack user groups, or Teams teams (their owners)."""
    user_ids: list[str]


@dataclass(frozen=True)
class ChannelBudgetSummary:
    limit_usd: str
    window: str
    starts_at: str | None
    ends_at: str | None
    spent_usd: str
    """Debited spend in the current window, markup included."""


@pydantic_dataclass(frozen=True)
class ChannelSummary:
    channel_id: str
    agent_name: str | None
    """The agent answering there: the channel's own, else the default."""
    environment_name: str | None
    """The environment its sessions run in, resolved the same way."""
    isolated: bool
    admins: ChannelAdmins
    """Who administers the channel on top of the server admins; both lists empty for nobody."""
    budget: ChannelBudgetSummary | None
    sealed: bool | None = Field(default=None, exclude_if=_omitted)
    """Read only from inside it; omitted unless the caller may read the access policy."""
    protected: bool | None = Field(default=None, exclude_if=_omitted)
    """Closed to members' turns; omitted unless the caller may read the access policy."""


@dataclass(frozen=True)
class TimedCreditSummary:
    remaining_usd: str
    ends_at: str


@dataclass(frozen=True)
class TenantSummary:
    balance_usd: str
    funding_mode: FundingMode
    default_agent: str | None
    """The agent that answers in a channel with no agent of its own."""
    channels: list[ChannelSummary]
    timed_credit: list[TimedCreditSummary]
    """Live promo credit that expires, soonest-ending first; part of ``balance_usd``."""


def _budget_summary(status: ChannelBudgetStatus) -> ChannelBudgetSummary:
    budget = status.budget
    return ChannelBudgetSummary(
        limit_usd=str(budget.limit_usd),
        window=budget.window,
        starts_at=budget.starts_at.isoformat() if budget.starts_at else None,
        ends_at=budget.ends_at.isoformat() if budget.ends_at else None,
        spent_usd=str(status.spent_usd),
    )


def build_channel_summaries(
    *,
    channel_rows: list[ChannelConfigRow],
    tenant_row: TenantConfigRow | None,
    default: DeploymentDefault,
    budgets: dict[str, ChannelBudgetStatus],
    admins: dict[str, ChannelAdminsRow],
    dm_channel_ids: set[str],
    policy: TenantAccessPolicy,
    viewer: ChannelViewer,
) -> list[ChannelSummary]:
    """One entry per channel with a setting, a budget, admins or isolation, by channel id.

    A DM's channel gets a config row when the DM starts; it is not a channel
    of the workspace, so ``dm_channel_ids`` are skipped. Each entry carries
    what `viewer`, a server admin, sees of it (`daimon.core.channel_overview`).
    """
    configs = {row.channel_id: row for row in channel_rows}
    summaries: list[ChannelSummary] = []
    listed = configs.keys() | budgets.keys() | admins.keys() | set(confidential_channels(policy))
    for channel_id in sorted(listed - dm_channel_ids):
        resolved = merge(channel=configs.get(channel_id), tenant=tenant_row, default=default)
        overview = build_channel_overview(
            channel_id,
            viewer=viewer,
            policy=policy,
            environment_name=resolved.environment_name,
            budget=budgets.get(channel_id),
            admins=admins.get(channel_id),
        )
        granted = overview.admins or ChannelAdminSet()
        summaries.append(
            ChannelSummary(
                channel_id=channel_id,
                agent_name=resolved.agent_name,
                environment_name=overview.environment_name,
                isolated=overview.isolated is True,
                admins=ChannelAdmins(
                    role_ids=list(granted.role_ids), user_ids=list(granted.user_ids)
                ),
                budget=_budget_summary(overview.budget) if overview.budget is not None else None,
                sealed=overview.sealed,
                protected=overview.protected,
            )
        )
    return summaries


async def load_tenant_summary(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
    now: datetime,
    with_access: bool = False,
) -> TenantSummary:
    """The tenant's summary; raises `StoreError` for an unknown tenant and
    `AccessPolicyUnreadable` when its access policy can't be read.

    ``with_access`` fills each channel's seal and protection: only for a
    caller that can already read the whole access policy."""
    tenant = await get_tenant(session, tenant_id)
    if tenant is None:
        raise StoreError(f"no tenant {tenant_id}")
    balance = await get_balance(session, tenant_id=tenant_id)
    tenant_row, channel_rows = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    budgets = {
        budget.channel_id: await load_budget_status(session, budget, now=now)
        for budget in await list_channel_budgets(session, tenant_id=tenant_id)
        if budget.platform == tenant.platform
    }
    admins = {
        row.channel_id: row
        for row in await list_channel_admins(session, tenant_id=tenant_id, platform=tenant.platform)
    }
    dm_channel_ids = await list_dm_channel_ids(session, tenant_id=tenant_id)
    policy = await load_access_policy(session, tenant_id=tenant_id)
    resolved_default = merge(channel=None, tenant=tenant_row, default=default)
    return TenantSummary(
        balance_usd=f"{balance:.2f}",
        funding_mode=tenant.funding_mode,
        default_agent=resolved_default.agent_name,
        channels=build_channel_summaries(
            channel_rows=channel_rows,
            tenant_row=tenant_row,
            default=default,
            budgets=budgets,
            admins=admins,
            dm_channel_ids=dm_channel_ids,
            policy=policy,
            viewer=ChannelViewer(is_server_admin=True, reads_access_policy=with_access),
        ),
        timed_credit=[
            TimedCreditSummary(
                remaining_usd=f"{credit.remaining_usd:.2f}", ends_at=credit.ends_at.isoformat()
            )
            for credit in await get_active_timed_credit(session, tenant_id=tenant_id, now=now)
        ],
    )
