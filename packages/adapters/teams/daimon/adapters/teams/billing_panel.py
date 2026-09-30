"""The `billing` command and its card actions: this month's usage, the balance and top-ups.

Mirrors Slack's `/billing`. A member sees their own spend and the balance; an
admin also sees tenant totals, the top spenders and top-up buttons. A top-up
click re-checks admin, creates a Stripe Checkout through the MCP server and
replaces the card with an `Action.OpenUrl` to it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import structlog
from daimon.adapters.teams.card_actions import (
    FAILED,
    button,
    card_actor,
    get_or_create_account,
    guarded,
    heading,
    replace_card,
    text_card,
    text_lines,
    toast,
)
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.billing_panel import (
    TOPUP_AMOUNTS,
    BillingPanelState,
    caller_line,
    create_checkout,
    estimate_turns,
    fmt_usd,
    load_billing_snapshot,
    month_start,
    period_label,
    spend_over_cap,
)
from daimon.core.errors import DaimonError
from daimon.core.observability import capture_exception_with_scope
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext
from microsoft_teams.cards import (
    Action,
    ActionSet,
    AdaptiveCard,
    CardElement,
    ExecuteAction,
    OpenUrlAction,
)

log = structlog.get_logger()

VERB = "billing"
ADMIN_ONLY = "Only an admin can top up credit."
UNKNOWN_AMOUNT = "That top-up amount is not offered."
NOT_CONFIGURED = (
    "Payments aren't configured for this organisation. "
    "Ask an operator about a manual credit top-up."
)
_TOP_SHOWN = 5


def _member_body(state: BillingPanelState, since: datetime) -> list[CardElement]:
    used = "no usage yet this period"
    if state.caller_spend or state.caller_turns:
        used = caller_line(state.caller_spend, state.caller_cap, state.caller_turns)
    over = " ⚠️ over cap" if spend_over_cap(state.caller_spend, state.caller_cap) else ""
    balance = fmt_usd(state.guild_balance_usd)
    return [
        heading("💸 Billing"),
        *text_lines(period_label(since), f"**You**{over}: {used}"),
        *text_lines(f"🏦 **Credit**: {balance} balance (top-ups are admin-only)"),
    ]


def _topup(amount: int, state: BillingPanelState) -> Action:
    turns = estimate_turns(amount, guild_spend=state.guild_spend, guild_turns=state.guild_turns)
    return button(VERB, f"${amount} · ≈{turns:,} turns", "topup", amount=str(amount))


def _admin_body(state: BillingPanelState, since: datetime) -> list[CardElement]:
    totals = (
        f"{period_label(since)} · organisation total {fmt_usd(state.guild_spend)} · "
        f"{state.guild_turns} turns · {state.guild_distinct_members} active members"
    )
    top = [
        f"{rank}. {row.display_name}{' (you)' if row.is_caller else ''} "
        f"{fmt_usd(row.cost_usd)} · {row.turn_count} turns"
        for rank, row in enumerate(state.member_rows[:_TOP_SHOWN], start=1)
    ]
    overflow = max(0, len(state.member_rows) - _TOP_SHOWN) + state.over_cap_count
    if overflow:
        top.append(f"{overflow} more members")
    balance = fmt_usd(state.guild_balance_usd)
    return [
        heading("💸 Billing · admin view"),
        *text_lines(totals, f"🏦 **Credit**: {balance} balance", "🏆 **Top spenders**"),
        *text_lines(*(top or ["no usage yet this period"]), "💳 **Top up credit**"),
        ActionSet(actions=[_topup(amount, state) for amount in TOPUP_AMOUNTS]),
    ]


def panel_card(state: BillingPanelState, *, since: datetime) -> AdaptiveCard:
    """The member view, or for an admin the tenant view with top-up buttons."""
    body = _admin_body(state, since) if state.is_admin else _member_body(state, since)
    return AdaptiveCard(body=body, fallback_text="Billing")


def _back() -> ExecuteAction:
    return button(VERB, "Back", "refresh")


def checkout_card(url: str, amount: int) -> AdaptiveCard:
    pay: list[Action] = [OpenUrlAction(title="Complete payment", url=url), _back()]
    body: list[CardElement] = [
        heading(f"💳 Top up ${amount}"),
        *text_lines("Complete the payment in your browser; the credit lands once Stripe confirms."),
        ActionSet(actions=pay),
    ]
    return AdaptiveCard(body=body, fallback_text="Complete payment")


class BillingPanel:
    """Handlers for the command and the panel buttons."""

    def __init__(self, runtime: TeamsRuntime) -> None:
        self._runtime = runtime

    async def command(self, context: CommandContext) -> None:
        await context.send_card(
            await self._panel(context.tenant_id, context.inbound.user_id, context.is_admin)
        )

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx.activity), toast(FAILED), "teams.billing.failed")

    async def _panel(self, tenant_id: uuid.UUID, user_id: str, is_admin: bool) -> AdaptiveCard:
        since = month_start(datetime.now(UTC))
        async with self._runtime.sessionmaker() as session:
            state = await load_billing_snapshot(
                session,
                tenant_id=tenant_id,
                platform_user_id=user_id,
                is_admin=is_admin,
                since=since,
            )
        return panel_card(state, since=since)

    async def _act(self, activity: AdaptiveCardInvokeActivity) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        data = activity.value.action.data
        if data.get("op") != "topup":
            return replace_card(await self._panel(actor.tenant_id, actor.user_id, actor.is_admin))
        if not actor.is_admin:
            return toast(ADMIN_ONLY)
        amount = next((a for a in TOPUP_AMOUNTS if str(a) == str(data.get("amount"))), None)
        if amount is None:
            return toast(UNKNOWN_AMOUNT)
        account_id = await get_or_create_account(self._runtime, actor)
        try:
            url = await create_checkout(
                self._runtime.http_client,
                settings=self._runtime.settings.mcp,
                account_id=account_id,
                amount=amount,
            )
        except (DaimonError, httpx.HTTPError) as exc:
            log.error("teams.billing.checkout_failed", tenant_id=str(actor.tenant_id), exc_info=exc)
            capture_exception_with_scope(exc)
            return replace_card(text_card("💸 Billing", NOT_CONFIGURED, back=_back()))
        return replace_card(checkout_card(url, amount))
