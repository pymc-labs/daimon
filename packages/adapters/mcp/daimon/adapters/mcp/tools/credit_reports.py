"""The web app's credit and feedback pages, from chat: a person's own use, the workspace
overview, spend by person, agent or channel, and what people said about answers."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._web_admin import agent_names, now, web_admin_of
from daimon.adapters.mcp.tools._web_url import WebUrl, tenant_links
from daimon.core import credit_report, credit_views
from daimon.core.credit_report import AgentLabel, DailyEntry, LedgerEntry, SpendBy, TurnEntry
from daimon.core.feedback_summary import (
    FeedbackNotAllowed,
    FeedbackOut,
    feedback_report,
    feedback_summary,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_REMOVED = "Removed agent"


@dataclass(frozen=True)
class SpendLine:
    id: str
    name: str
    turns: int
    usd: float
    kind: str | None = None


@dataclass(frozen=True)
class MyCredits:
    by_agent: list[SpendLine]
    turns: list[TurnEntry]
    spent_usd: float
    turn_count: int
    web_url: WebUrl = None


@dataclass(frozen=True)
class CreditsOverview:
    balance_usd: float
    spent_usd: float
    """The last 30 days."""
    turns: int
    spent_month_usd: float
    projected_month_usd: float
    daily: list[DailyEntry]
    ledger: list[LedgerEntry]
    funding: Literal["prepaid", "operator_funded"]
    web_url: WebUrl = None


@dataclass(frozen=True)
class CreditsSpend:
    by: SpendBy
    rows: list[SpendLine]
    web_url: WebUrl = None


@dataclass(frozen=True)
class FeedbackSummary:
    report: FeedbackOut
    web_url: WebUrl = None


def _line(entry: credit_report.SpendEntry) -> SpendLine:
    return SpendLine(entry.id, entry.name, entry.turns, entry.usd, entry.kind)


def _labels(runtime: McpRuntime, tenant_id: uuid.UUID) -> credit_report.AgentLabels:
    async def labels(ids: Collection[str]) -> Mapping[str, AgentLabel]:
        names = await agent_names(runtime, tenant_id)
        return {i: AgentLabel(names.get(i, _REMOVED)) for i in ids}

    return labels


async def _stored_names(
    names: dict[tuple[str, str], str], _channels: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], str]:
    return names


async def _mine_impl(
    runtime: McpRuntime, auth: AuthIdentity, period: credit_views.Period
) -> MyCredits:
    if auth.is_external_agent or auth.hold is not None or auth.client:
        raise ToolError("This tool acts only on a person's own credits.")
    if not auth.platform_user_id:
        raise ToolError("The caller has no chat user.")
    report = await credit_report.my_credits(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        platform_user_id=auth.platform_user_id,
        period=period,
        now=now(),
        markup=runtime.settings.billing.markup,
        agent_labels=_labels(runtime, auth.tenant_id),
    )
    links = tenant_links(runtime, auth)
    return MyCredits(
        by_agent=[_line(e) for e in report.by_agent],
        turns=report.turns,
        spent_usd=report.spent_usd,
        turn_count=report.turn_count,
        web_url=links.credits() if links else None,
    )


async def _overview_impl(runtime: McpRuntime, auth: AuthIdentity) -> CreditsOverview:
    actor = await web_admin_of(runtime, auth)
    report = await credit_report.overview_report(
        runtime.session_factory,
        tenant_id=actor.tenant_id,
        now=now(),
        markup=runtime.settings.billing.markup,
    )
    links = tenant_links(runtime, auth)
    return CreditsOverview(
        balance_usd=report.balance_usd,
        spent_usd=report.spent_usd,
        turns=report.turns,
        spent_month_usd=report.spent_month_usd,
        projected_month_usd=report.projected_month_usd,
        daily=report.daily,
        ledger=report.ledger,
        funding=report.funding,
        web_url=links.credits() if links else None,
    )


async def _spend_impl(
    runtime: McpRuntime, auth: AuthIdentity, by: SpendBy, period: credit_views.Period
) -> CreditsSpend:
    actor = await web_admin_of(runtime, auth)
    rows = await credit_report.spend_breakdown(
        runtime.session_factory,
        tenant_id=actor.tenant_id,
        by=by,
        period=period,
        now=now(),
        markup=runtime.settings.billing.markup,
        agent_labels=_labels(runtime, actor.tenant_id),
        channel_names=_stored_names,
    )
    links = tenant_links(runtime, auth)
    return CreditsSpend(
        by=by, rows=[_line(e) for e in rows], web_url=links.spend() if links else None
    )


async def _feedback_impl(runtime: McpRuntime, auth: AuthIdentity, days: int) -> FeedbackSummary:
    actor = await web_admin_of(runtime, auth)
    if not 1 <= days <= 365:
        raise ToolError("days must be from 1 to 365.")
    until = now()
    names = await agent_names(runtime, actor.tenant_id)
    try:
        async with runtime.session_factory() as db:
            found = await feedback_summary(
                db,
                subject=mcp_subject(auth, is_admin=auth.is_admin),
                tenant_id=actor.tenant_id,
                since=until - timedelta(days=days),
                until=until + timedelta(seconds=1),
            )
    except FeedbackNotAllowed as err:
        raise ToolError(str(err)) from err
    links = tenant_links(runtime, auth)
    return FeedbackSummary(
        report=feedback_report(found, names=names, days=days),
        web_url=links.feedback() if links else None,
    )


def register_credit_report_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def get_my_credits(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, period: credit_views.Period = "30d"
    ) -> MyCredits:
        """The caller's own use in billed dollars over ``period`` (``30d``, ``this`` month or
        ``last`` month), by agent, and their latest turns."""
        return await _mine_impl(runtime, await _auth(ctx), period)

    @mcp.tool(tags={"admin"})
    async def get_credits_overview(ctx: Context) -> CreditsOverview:  # pyright: ignore[reportUnusedFunction]
        """The workspace's credit balance, spend over the last 30 days and this month with
        a projection, daily spend and the credit history. Admin-only."""
        return await _overview_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin"})
    async def get_credits_spend(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, by: SpendBy = "person", period: credit_views.Period = "30d"
    ) -> CreditsSpend:
        """The workspace's spend in billed dollars over ``period``, one row per ``person``,
        ``agent`` or ``channel``, biggest first. Admin-only."""
        return await _spend_impl(runtime, await _auth(ctx), by, period)

    @mcp.tool(tags={"admin"})
    async def get_feedback_summary(ctx: Context, days: int = 30) -> FeedbackSummary:  # pyright: ignore[reportUnusedFunction]
        """Thumbs and the reasons picked per agent over the last ``days`` days, as counts.
        A count of one or two is withheld (null), as it could point to a voter. Admin-only."""
        return await _feedback_impl(runtime, await _auth(ctx), days)


__all__ = ["register_credit_report_tools"]
