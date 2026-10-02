"""Tenant summary: the balance, funding mode and every configured channel in one read.

For server admins and operator tokens holding ``tenant:read``, so an
integration can show a workspace's state without one call per channel. A
channel is listed when it has its own agent or environment setting, a budget
or channel admins. The private channels DM conversations run in are left out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.channel_budget import ChannelBudgetStatus, load_budget_status
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow, merge
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.channel_budgets import list_channel_budgets
from daimon.core.stores.direct_messages import list_dm_channel_ids
from daimon.core.stores.domain import FundingMode
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.tenant_ledger import get_balance
from daimon.core.stores.tenants import get_tenant
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class ChannelAdmins:
    role_ids: list[str]
    """Discord role ids; always empty on Slack, which has no roles."""
    user_ids: list[str]


@dataclass(frozen=True)
class ChannelBudgetSummary:
    limit_usd: str
    window: str
    starts_at: str | None
    ends_at: str | None
    spent_usd: str
    """Debited spend in the current window, markup included."""


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class TenantSummary:
    balance_usd: str
    funding_mode: FundingMode
    default_agent: str | None
    """The agent that answers in a channel with no agent of its own."""
    channels: list[ChannelSummary]


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
    admins: dict[str, ChannelAdmins],
    dm_channel_ids: set[str],
) -> list[ChannelSummary]:
    """One entry per channel with a setting, a budget or admins, ordered by channel id.

    A DM's channel gets a config row when the DM starts; it is not a channel
    of the workspace, so ``dm_channel_ids`` are skipped.
    """
    configs = {row.channel_id: row for row in channel_rows}
    summaries: list[ChannelSummary] = []
    for channel_id in sorted((configs.keys() | budgets.keys() | admins.keys()) - dm_channel_ids):
        resolved = merge(channel=configs.get(channel_id), tenant=tenant_row, default=default)
        status = budgets.get(channel_id)
        summaries.append(
            ChannelSummary(
                channel_id=channel_id,
                agent_name=resolved.agent_name,
                environment_name=resolved.environment_name,
                # A later isolation feature fills this in build_channel_summaries.
                isolated=False,
                admins=admins.get(channel_id, ChannelAdmins(role_ids=[], user_ids=[])),
                budget=_budget_summary(status) if status is not None else None,
            )
        )
    return summaries


async def _get_tenant_summary_impl(runtime: McpRuntime, auth: AuthIdentity) -> TenantSummary:
    require_scope(auth, "tenant:read")
    _require_admin(auth)
    now = datetime.now(UTC)
    async with runtime.session_factory() as session:
        tenant = await get_tenant(session, auth.tenant_id)
        if tenant is None:
            raise ToolError("internal: the caller's tenant no longer exists")
        balance = await get_balance(session, tenant_id=auth.tenant_id)
        tenant_row, channel_rows = await list_propagations_for_tenant(
            session, tenant_id=auth.tenant_id
        )
        budgets = {
            budget.channel_id: await load_budget_status(session, budget, now=now)
            for budget in await list_channel_budgets(session, tenant_id=auth.tenant_id)
            if budget.platform == tenant.platform
        }
        admins = {
            row.channel_id: ChannelAdmins(role_ids=list(row.role_ids), user_ids=list(row.user_ids))
            for row in await list_channel_admins(
                session, tenant_id=auth.tenant_id, platform=tenant.platform
            )
        }
        dm_channel_ids = await list_dm_channel_ids(session, tenant_id=auth.tenant_id)
    default = merge(channel=None, tenant=tenant_row, default=runtime.deployment_default)
    return TenantSummary(
        balance_usd=f"{balance:.2f}",
        funding_mode=tenant.funding_mode,
        default_agent=default.agent_name,
        channels=build_channel_summaries(
            channel_rows=channel_rows,
            tenant_row=tenant_row,
            default=runtime.deployment_default,
            budgets=budgets,
            admins=admins,
            dm_channel_ids=dm_channel_ids,
        ),
    )


def register_tenant_summary_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("tenant:read")})
    async def get_tenant_summary(ctx: Context) -> TenantSummary:  # pyright: ignore[reportUnusedFunction]
        """Summarize this server or workspace: balance, funding mode and each channel. Admin-only.

        Lists every channel with its own agent or environment setting, a
        spending budget or channel admins, with the agent and environment that
        apply there, the roles and members administering it, and the budget's
        limit, window and spend (``budget`` is null without one). Money is a
        decimal string.
        """
        return await _get_tenant_summary_impl(runtime, await _auth(ctx))
