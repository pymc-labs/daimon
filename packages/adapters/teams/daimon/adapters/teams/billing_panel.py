"""The `billing` command and its card actions: this month's usage, the credit and top-ups.

Mirrors Slack's `/billing`, in the same words. The card is a stack of
containers set apart by separators: the month, a member's own use, the credit
left, the channel's budget, then the actions. A member sees their own use and
the credit; an admin also sees the month's spend, the top spenders and every
channel budget, and gets "Add credit" (amount buttons; a click re-checks admin,
creates a Stripe Checkout through the MCP server and replaces the card with an
`Action.OpenUrl` to it) and "Redeem code" while a code is redeemable. "Expiry
dates" opens a hidden section listing when each part of the timed credit
expires.

The top spenders are named from the rosters of the teams the bot is installed
in (`teams_installations`): Teams has no app-only way to name a person from
their Entra id without a tenant-wide Graph permission, but the Bot Framework
answers for any member of a team the bot is in. Someone in none of those teams
keeps the `User XXXX` label. Names are plain text, never an `<at>` mention.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import cast

import httpx
import structlog
from daimon.adapters.teams.card_actions import (
    FAILED,
    Actor,
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
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.billing_panel import (
    ADD_CREDIT,
    ASK_ADMIN,
    CHANNEL_BUDGET,
    CHANNEL_BUDGETS,
    CHANNEL_BUDGETS_SHOWN,
    EXPIRY_DATES,
    EXPIRY_INTRO,
    NOTHING_USED,
    REDEEM_CODE,
    TITLE,
    TOP_SPENDERS,
    TOP_SPENDERS_SHOWN,
    TOPUP_AMOUNTS,
    YOU,
    BillingPanelState,
    MemberRow,
    admin_summary,
    caller_line,
    channel_budget_line,
    channel_budget_phrase,
    create_checkout,
    credit_headline,
    estimate_turns,
    expiry_rows,
    load_billing_snapshot,
    month_label,
    month_start,
    more_channel_budgets,
    more_spenders,
    spend_over_cap,
    spender_line,
    timed_credit_note,
)
from daimon.core.errors import DaimonError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.panel_audit import record_panel_write
from daimon.core.promo_codes import describe_refusal
from daimon.core.promo_credit import PromoRedeemed, PromoRedeemRefused, redeem_promo_code
from daimon.core.stores.teams_installations import list_teams_installations
from daimon.core.teams_bot_framework import SERVICE_URL
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext, App
from microsoft_teams.cards import (
    Action,
    ActionSet,
    AdaptiveCard,
    CardElement,
    Container,
    ExecuteAction,
    OpenUrlAction,
    ShowCardAction,
    TextBlock,
    TextInput,
    ToggleVisibilityAction,
)

log = structlog.get_logger()

VERB = "billing"
ADMIN_ONLY = "Only an admin can top up credit."
UNKNOWN_AMOUNT = "That top-up amount is not offered."
NOT_CONFIGURED = (
    "Payments aren't configured for this organisation. "
    "Ask an operator about a manual credit top-up."
)
REDEEM_ADMIN_ONLY = "Only an admin can redeem a promo code."
EXPIRY_ID = "billing-expiry"
ENTER_CODE = "Enter a promo code."
CODE_INPUT = "code"
# The panel waits at most this long for roster lookups, all together.
NAME_LOOKUP_TIMEOUT_S = 2.0
# Team rosters tried per person, in `list_teams_installations` order.
_ROSTER_TEAMS = 5
_MARKDOWN = re.compile(r"([\\*_`~\[\]])")

RosterName = Callable[[str, str], Awaitable[str | None]]
"""(team id, Entra object id) -> the person's name on that team's roster, or None."""


def card_time(moment: datetime) -> str:
    """A moment Teams shows in each reader's own timezone."""
    iso = f"{moment.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
    return f"{{{{DATE({iso}, SHORT)}}}} {{{{TIME({iso})}}}}"


def card_date(moment: datetime) -> str:
    """A date Teams shows in each reader's own timezone."""
    return f"{{{{DATE({moment.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}, SHORT)}}}}"


def _details(*lines: str) -> list[CardElement]:
    """Small grey lines."""
    return [
        TextBlock(text=line, is_subtle=True, size="Small", spacing="None", wrap=True)
        for line in lines
    ]


def _block(*items: CardElement, element_id: str | None = None, hidden: bool = False) -> Container:
    """One section of the card, set apart from the one above."""
    return Container(
        items=list(items),
        separator=True,
        spacing="Large",
        id=element_id,
        is_visible=False if hidden else None,
    )


def _header(title: str, subtext: str) -> Container:
    return Container(items=[heading(title), *_details(subtext)])


def _credit(state: BillingPanelState) -> list[CardElement]:
    """The total as the biggest text on the card, the words under it, and the timed credit."""
    figure, words = credit_headline(state.guild_balance_usd)
    items: list[CardElement] = [TextBlock(text=figure, size="ExtraLarge", weight="Bolder")]
    if words is not None:
        items.append(TextBlock(text=words, spacing="None", wrap=True))
    details = [note] if (note := timed_credit_note(state.timed_credit)) else []
    if not state.is_admin:
        details.append(ASK_ADMIN)
    return items + _details(*details)


def plain_name(name: str) -> str:
    """A roster name as literal card text: markdown escaped, no `<at>` tag, one line."""
    flat = " ".join(name.replace("<", "").replace(">", "").split())
    return _MARKDOWN.sub(r"\\\1", flat)


def sdk_roster_name(app: App) -> RosterName:
    """Look a person up on a team's roster over the Bot Framework; no Graph permission needed."""

    async def roster_name(team_id: str, aad_object_id: str) -> str | None:
        conversations = app.api.from_service_url(SERVICE_URL).conversations
        member = await conversations.get_member_by_id(team_id, aad_object_id)
        if (member.aad_object_id or "").lower() != aad_object_id.lower():
            return None
        return member.name or None

    return roster_name


async def roster_names(
    roster_name: RosterName,
    *,
    team_ids: Sequence[str],
    user_ids: Sequence[str],
    timeout_s: float = NAME_LOOKUP_TIMEOUT_S,
) -> dict[str, str]:
    """Each person's name from the first team roster that has them.

    People are looked up concurrently; whoever is unresolved after ``timeout_s``
    is left out, as is anyone no roster has (a 404 for someone who left).
    """

    async def lookup(user_id: str) -> str | None:
        for team_id in team_ids[:_ROSTER_TEAMS]:
            try:
                if name := await roster_name(team_id, user_id):
                    return name
            except TEAMS_SEND_ERRORS as err:
                log.info("teams.billing.roster_lookup_failed", error=type(err).__name__)
        return None

    if not team_ids or not user_ids:
        return {}
    tasks = {asyncio.create_task(lookup(user_id)): user_id for user_id in user_ids}
    done, pending = await asyncio.wait(tasks, timeout=timeout_s)
    for task in pending:
        task.cancel()
    if pending:
        log.info("teams.billing.roster_lookup_timed_out", unresolved=len(pending))
    return {
        tasks[task]: name
        for task in done
        if not task.cancelled() and task.exception() is None and (name := task.result())
    }


def redeemed_text(result: PromoRedeemed) -> str:
    amount = f"**${result.amount_usd:,.2f}**"
    if result.credit_ends_at is None:
        return f"🎟️ Redeemed {amount} of credit. Balance: **${result.balance_usd:,.2f}**."
    window = f"until {card_time(result.credit_ends_at)}"
    if not result.granted and result.credit_starts_at is not None:
        window = f"from {card_time(result.credit_starts_at)} {window}"
    return (
        f"🎟️ Redeemed {amount} of timed credit, usable {window}. "
        "It is spent before other credit, and what is left then expires."
    )


def _topup(amount: int, state: BillingPanelState) -> Action:
    turns = estimate_turns(amount, guild_spend=state.guild_spend, guild_turns=state.guild_turns)
    return button(VERB, f"${amount} (≈ {turns:,} turns)", "topup", amount=str(amount))


def _sub_card(*items: CardElement) -> AdaptiveCard:
    return AdaptiveCard(body=list(items))


def _actions(state: BillingPanelState) -> list[Action]:
    """`Add credit` and `Redeem code` for an admin; `Expiry dates` with timed credit."""
    actions: list[Action] = []
    if state.is_admin:
        amounts = ActionSet(actions=[_topup(amount, state) for amount in TOPUP_AMOUNTS])
        actions.append(ShowCardAction(title=ADD_CREDIT, card=_sub_card(amounts)))
        if state.has_redeemable_promo_code:
            code = TextInput(id=CODE_INPUT, placeholder="XXXXX-XXXXX-XXXXX-XXXXX", max_length=100)
            redeem = ActionSet(actions=[button(VERB, "Redeem", "redeem")])
            actions.append(ShowCardAction(title=REDEEM_CODE, card=_sub_card(code, redeem)))
    if state.timed_credit:
        actions.append(ToggleVisibilityAction(title=EXPIRY_DATES, target_elements=[EXPIRY_ID]))
    return actions


def _titled(
    title: str, *items: CardElement, element_id: str | None = None, hidden: bool = False
) -> Container:
    """A section with a bold title, set apart from the one above."""
    label = TextBlock(text=title, weight="Bolder", wrap=True)
    return _block(label, *items, element_id=element_id, hidden=hidden)


def _spenders(state: BillingPanelState) -> Container:
    """`Top spenders` by roster name, then a grey `+ N more`."""
    rows = [
        spender_line(rank, plain_name(row.display_name), cost=row.cost_usd, is_caller=row.is_caller)
        for rank, row in enumerate(state.member_rows[:TOP_SPENDERS_SHOWN], start=1)
    ] or [NOTHING_USED]
    items: list[CardElement] = _rows(*rows)
    if overflow := more_spenders(len(state.member_rows), state.over_cap_count):
        items += _details(f"+ {overflow} more")
    return _titled(TOP_SPENDERS, *items)


def _channel_budgets(state: BillingPanelState, now: datetime) -> Container:
    """`Channel budgets`, most used first, five then a grey `+ N more`."""
    lines = [
        channel_budget_line(status, label=f"Channel `{status.budget.channel_id}`", now=now)
        for status in state.channel_budgets[:CHANNEL_BUDGETS_SHOWN]
    ]
    items: list[CardElement] = _rows(*lines)
    if more := more_channel_budgets(state):
        items += _details(f"+ {more} more")
    return _titled(CHANNEL_BUDGETS, *items)


def _rows(*lines: str) -> list[CardElement]:
    """List rows, close together."""
    return [TextBlock(text=line, spacing="Small", wrap=True) for line in lines]


def _panel_body(state: BillingPanelState, since: datetime, now: datetime) -> list[CardElement]:
    subtext = (
        admin_summary(since, spend=state.guild_spend, people=state.guild_distinct_members)
        if state.is_admin
        else month_label(since)
    )
    body: list[CardElement] = [_header(TITLE, subtext)]
    if not state.is_admin:
        own = caller_line(state.caller_spend, state.caller_cap, state.caller_turns)
        over = "  ⚠️ Over your cap" if spend_over_cap(state.caller_spend, state.caller_cap) else ""
        body.append(_titled(f"{YOU}{over}", *_rows(own)))
    body.append(_block(*_credit(state)))
    if state.channel_budget is not None:
        phrase = channel_budget_phrase(state.channel_budget, now=now)
        body.append(_titled(CHANNEL_BUDGET, *_rows(phrase)))
    if state.is_admin:
        body.append(_spenders(state))
        if state.channel_budgets:
            body.append(_channel_budgets(state, now))
    if state.timed_credit:
        rows = expiry_rows(state.timed_credit, when=card_date)
        body.append(_titled(EXPIRY_INTRO, *_rows(*rows), element_id=EXPIRY_ID, hidden=True))
    if actions := _actions(state):
        body.append(ActionSet(actions=actions, separator=True, spacing="Large"))
    return body


def panel_card(
    state: BillingPanelState,
    *,
    since: datetime,
    now: datetime | None = None,
    notice: str | None = None,
) -> AdaptiveCard:
    """The member view, or for an admin the tenant view with its admin actions."""
    body = _panel_body(state, since, now or datetime.now(UTC))
    if notice is not None:
        body = [*text_lines(notice), *body]
    return AdaptiveCard(body=body, fallback_text=TITLE)


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

    def __init__(self, runtime: TeamsRuntime, *, roster_name: RosterName | None = None) -> None:
        self._runtime = runtime
        self._roster_name = roster_name

    async def command(self, context: CommandContext) -> None:
        await context.send_card(
            await self._panel(context.tenant_id, context.inbound.user_id, context.is_admin)
        )

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx.activity), toast(FAILED), "teams.billing.failed")

    async def _panel(
        self, tenant_id: uuid.UUID, user_id: str, is_admin: bool, *, notice: str | None = None
    ) -> AdaptiveCard:
        now = datetime.now(UTC)
        since = month_start(now)
        async with self._runtime.sessionmaker() as session:
            state = await load_billing_snapshot(
                session,
                tenant_id=tenant_id,
                platform_user_id=user_id,
                is_admin=is_admin,
                since=since,
                now=now,
                platform="teams",
            )
            teams = await list_teams_installations(session, tenant_id=tenant_id)
        state = await self._named(state, [team.team_id for team in teams])
        return panel_card(state, since=since, now=now, notice=notice)

    async def _named(self, state: BillingPanelState, team_ids: list[str]) -> BillingPanelState:
        """The state with the shown top spenders' roster names in place of `User XXXX`."""
        shown = state.member_rows[:TOP_SPENDERS_SHOWN]
        if self._roster_name is None or not shown:
            return state
        names = await roster_names(
            self._roster_name,
            team_ids=team_ids,
            user_ids=[row.platform_user_id for row in shown],
        )
        rows: tuple[MemberRow, ...] = tuple(
            dataclasses.replace(row, display_name=names.get(row.platform_user_id, row.display_name))
            for row in shown
        )
        return dataclasses.replace(state, member_rows=rows + state.member_rows[TOP_SPENDERS_SHOWN:])

    async def _act(self, activity: AdaptiveCardInvokeActivity) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        data = activity.value.action.data
        if data.get("op") == "redeem":
            return await self._redeem(actor, str(cast(object, data.get(CODE_INPUT)) or ""))
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

    async def _redeem(self, actor: Actor, code: str) -> AdaptiveCardInvokeResponse:
        """Redeem for a live admin; a refusal leaves the card, and the typed code, as it was."""
        audit = functools.partial(
            record_panel_write,
            self._runtime.sessionmaker,
            tenant_id=actor.tenant_id,
            platform="teams",
            platform_user_id=actor.user_id,
            op="promo_redeem",
        )
        if not actor.is_admin:
            await audit(outcome="denied", reason="needs_admin")
            return toast(REDEEM_ADMIN_ONLY)
        if not code.strip():
            return toast(ENTER_CODE)
        result = await redeem_promo_code(
            self._runtime.sessionmaker,
            tenant_id=actor.tenant_id,
            account_id=await get_or_create_account(self._runtime, actor),
            code=code,
            now=datetime.now(UTC),
        )
        if isinstance(result, PromoRedeemRefused):
            await audit(outcome="denied", reason=f"promo:{result.reason}")
            return toast(describe_refusal(result.reason))
        await audit(outcome="allowed", reason="completed")
        card = await self._panel(actor.tenant_id, actor.user_id, True, notice=redeemed_text(result))
        return replace_card(card)
