"""The `billing` command and its card actions: this month's usage, the credit and top-ups.

Mirrors Slack's `/billing`, in the same words. The card is a stack of
containers set apart by separators: the month, a member's own use, the credit
left, the channel's budget, then the actions. A member sees their own use and
the credit; an admin also sees the month's spend, the top spenders and every
channel budget, and gets "Add credit" (amount buttons; a click re-checks admin,
creates a Stripe Checkout through the MCP server and replaces the card with an
`Action.OpenUrl` to it) and "Redeem code" while a code is redeemable. "Expiry
dates" opens a hidden section listing when each part of the timed credit
expires. An admin's "Look up a person" is Teams' people picker (an
`Input.ChoiceSet` searching the organisation's directory, which submits the
person's Entra object id); its click re-checks admin and redraws the card with
that person's spend this month under the actions.

The top spenders are named from the rosters of the teams the bot is installed
in (`teams_installations`): Teams has no app-only way to name a person from
their Entra id without a tenant-wide Graph permission, but the Bot Framework
answers for any member of a team the bot is in. Someone no roster has, or
whose lookup times out, gets the name stored from their last message or click
(`daimon.core.platform_names`); someone never named to us reads
`Name unavailable`. Names are plain text, never an `<at>` mention, which would
notify them.

Channel budgets are named the same way: the channel listings of those teams,
else the name stored from a message there, else `General` for a team's own
id; never the raw `19:…` id.

Typed in a channel, the card is answered in the 1:1 chat with that channel's
budget under "This channel", as Slack's and Discord's panels show the budget
of the channel they were opened in. The channel is held here under a token its
buttons carry, honoured only for the person who typed the command, so a forged
click cannot read another channel's budget; after a restart a refresh drops
the line.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import re
import secrets
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
from daimon.adapters.teams.identity import DENIED, canonical_uuid
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
    LOOK_UP,
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
    expiry_rows,
    load_billing_snapshot,
    lookup_line,
    month_label,
    month_start,
    more_channel_budgets,
    more_spenders,
    spend_over_cap,
    spender_line,
    stored_name_labels,
    timed_credit_note,
)
from daimon.core.errors import DaimonError
from daimon.core.observability import capture_exception_with_scope
from daimon.core.panel_audit import record_panel_write
from daimon.core.platform_names import (
    KnownName,
    remember_channel_names,
    remember_user_names,
    resolve_names,
)
from daimon.core.promo_codes import describe_refusal
from daimon.core.promo_credit import PromoRedeemed, PromoRedeemRefused, redeem_promo_code
from daimon.core.stores.platform_names import get_channel_names
from daimon.core.stores.teams_installations import list_teams_installations
from daimon.core.stores.usage_events import (
    cost_for_user_in_tenant_since,
    turn_count_for_user_in_tenant_since,
)
from daimon.core.teams_bot_framework import SERVICE_URL
from microsoft_teams.api import AdaptiveCardInvokeActivity, AdaptiveCardInvokeResponse
from microsoft_teams.apps import ActivityContext, App
from microsoft_teams.cards import (
    Action,
    ActionSet,
    AdaptiveCard,
    CardElement,
    ChoiceSetInput,
    Column,
    ColumnSet,
    Container,
    ExecuteAction,
    OpenUrlAction,
    QueryData,
    ShowCardAction,
    TextBlock,
    TextInput,
    ToggleVisibilityAction,
)

log = structlog.get_logger()

VERB = "billing"
ADMIN_ONLY = "Only an admin can top up credit."
UNKNOWN_AMOUNT = "That top-up amount is not offered."
NOT_CONFIGURED = "Card payments aren't set up here.\n\nAsk the team running Daimon to add credit."
REDEEM_ADMIN_ONLY = "Only an admin can redeem a promo code."
LOOKUP_ADMIN_ONLY = "Only an admin can look up a person's spend."
PICK_A_PERSON = "Pick a person to look up."
PERSON_INPUT = "person"
# Teams' people picker: the choices come from the organisation's directory.
# https://learn.microsoft.com/microsoftteams/platform/task-modules-and-cards/cards/people-picker
PEOPLE_DATASET = "graph.microsoft.com/users"
EXPIRY_ID = "billing-expiry"
ENTER_CODE = "Enter a promo code."
CODE_INPUT = "code"
# The panel waits at most this long for roster lookups, all together.
NAME_LOOKUP_TIMEOUT_S = 2.0
# Someone no roster, message or click ever named to us.
NAME_UNAVAILABLE = "Name unavailable"
# A budgeted channel no listing or message ever named to us.
CHANNEL_NAME_UNAVAILABLE = "Channel name unavailable"
GENERAL = "General"
# Team rosters tried per person, in `list_teams_installations` order.
_ROSTER_TEAMS = 5
_MARKDOWN = re.compile(r"([\\*_`~\[\]])")
# Every button carries the card's place token ("" for none), so a click keeps its channel.
PLACE = "place"
# Channels held for "This channel" across clicks, per person; their oldest is dropped first.
# Per person, so someone typing `billing` again and again never drops another's channel.
_MAX_PLACES = 8

RosterName = Callable[[str, str], Awaitable[str | None]]
"""(team id, Entra object id) -> the person's name on that team's roster, or None."""

TeamChannels = Callable[[str], Awaitable[dict[str, str]]]
"""Team id -> every channel of that team the bot can list, by id, with its name."""


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


def _header(title: str, *subtext: str) -> Container:
    return Container(items=[heading(title), *_details(*subtext)])


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


def sdk_team_channels(app: App) -> TeamChannels:
    """List a team's channels over the Bot Framework, General's name filled in."""

    async def team_channels(team_id: str) -> dict[str, str]:
        channels = await app.api.from_service_url(SERVICE_URL).teams.get_conversations(team_id)
        names = {channel.id: channel.name for channel in channels if channel.id and channel.name}
        # Teams names General nowhere: its id is the team's.
        return {team_id: GENERAL} | names

    return team_channels


async def roster_names(
    roster_name: RosterName,
    *,
    team_ids: Sequence[str],
    user_ids: Sequence[str],
    stored: Mapping[str, str] | None = None,
    timeout_s: float = NAME_LOOKUP_TIMEOUT_S,
) -> tuple[dict[str, str], dict[str, KnownName]]:
    """Each person's label, and the names the rosters gave, to remember.

    A person's name comes from the first team roster that has them, else from
    ``stored``. People are looked up concurrently; whoever is unresolved after
    ``timeout_s`` gets their stored name, as does anyone no roster has (a 404
    for someone who left). Someone with neither is left out.
    """

    async def lookup(user_id: str) -> KnownName | None:
        for team_id in team_ids[:_ROSTER_TEAMS]:
            try:
                if name := await roster_name(team_id, user_id):
                    return KnownName(display_name=name)
            except TEAMS_SEND_ERRORS as err:
                log.info("teams.billing.roster_lookup_failed", error=type(err).__name__)
        return None

    return await resolve_names(
        user_ids,
        live=lookup if team_ids else None,
        stored=stored or {},
        timeout_s=timeout_s,
        log_event="teams.billing.roster_lookup_timed_out",
    )


async def channel_labels(
    team_channels: TeamChannels | None,
    *,
    team_ids: Sequence[str],
    channel_ids: Sequence[str],
    stored: Mapping[str, str],
    timeout_s: float = NAME_LOOKUP_TIMEOUT_S,
) -> tuple[dict[str, str], dict[str, str]]:
    """Each channel's name, and every name the listings gave, to remember.

    The installed teams' channel listings run concurrently under ``timeout_s``;
    a channel they do not name gets its ``stored`` name, else `General` when
    its id is a team's, else `Channel name unavailable`.
    """
    listed: dict[str, str] = {}

    async def listing(team_id: str) -> dict[str, str]:
        assert team_channels is not None
        try:
            return await team_channels(team_id)
        except TEAMS_SEND_ERRORS as err:
            log.info("teams.billing.channel_listing_failed", error=type(err).__name__)
            return {}

    if team_channels is not None and team_ids and channel_ids:
        tasks = [asyncio.create_task(listing(team_id)) for team_id in team_ids[:_ROSTER_TEAMS]]
        done, pending = await asyncio.wait(tasks, timeout=timeout_s)
        for task in pending:
            task.cancel()
        if pending:
            log.info("teams.billing.channel_listing_timed_out", unresolved=len(pending))
        for task in tasks:
            if task in done:
                listed |= task.result()
    teams = set(team_ids)
    labels = {
        channel_id: listed.get(channel_id)
        or stored.get(channel_id)
        or (GENERAL if channel_id in teams else CHANNEL_NAME_UNAVAILABLE)
        for channel_id in channel_ids
    }
    return labels, listed


async def _no_roster(_team_id: str, _user_id: str) -> str | None:
    return None


def redeemed_text(result: PromoRedeemed) -> str:
    amount = f"**${result.amount_usd:,.2f}**"
    if result.credit_ends_at is None:
        return f"🎟️ Added {amount} of credit.\n\nBalance: **${result.balance_usd:,.2f}**"
    if not result.granted and result.credit_starts_at is not None:
        return (
            f"🎟️ Credit scheduled: {amount} from {card_time(result.credit_starts_at)} "
            f"until {card_time(result.credit_ends_at)}."
        )
    return (
        f"🎟️ Added {amount} of credit.\n\n"
        "Used before credit with no expiry. Anything unused expires "
        f"{card_time(result.credit_ends_at)}."
    )


def _topup(amount: int, state: BillingPanelState, place: str | None) -> Column:
    """A payment button labelled with its amount."""
    pay = button(VERB, f"${amount}", "topup", amount=str(amount), place=place or "")
    return Column(width="auto", items=[ActionSet(actions=[pay])])


@dataclasses.dataclass(frozen=True)
class Lookup:
    """A "Look up a person" pick: their name as card text and their spend line."""

    name: str
    line: str


def _sub_card(*items: CardElement) -> AdaptiveCard:
    return AdaptiveCard(body=list(items))


def _actions(state: BillingPanelState, place: str | None) -> list[Action]:
    """`Add credit` and `Redeem code` for an admin; `Expiry dates` with timed credit."""
    actions: list[Action] = []
    if state.is_admin:
        amounts = ColumnSet(columns=[_topup(amount, state, place) for amount in TOPUP_AMOUNTS])
        actions.append(ShowCardAction(title=ADD_CREDIT, card=_sub_card(amounts)))
        if state.has_redeemable_promo_code:
            code = TextInput(id=CODE_INPUT, placeholder="XXXXX-XXXXX-XXXXX-XXXXX", max_length=100)
            redeem = ActionSet(actions=[button(VERB, "Redeem", "redeem", place=place or "")])
            actions.append(ShowCardAction(title=REDEEM_CODE, card=_sub_card(code, redeem)))
    if state.timed_credit:
        actions.append(ToggleVisibilityAction(title=EXPIRY_DATES, target_elements=[EXPIRY_ID]))
    if state.is_admin:
        picker = ChoiceSetInput(
            id=PERSON_INPUT,
            label=LOOK_UP,
            placeholder="Search for a person",
            choices=[],
            choices_data=QueryData(dataset=PEOPLE_DATASET),
        )
        look = ActionSet(actions=[button(VERB, "Look up", "lookup", place=place or "")])
        actions.append(ShowCardAction(title=LOOK_UP, card=_sub_card(picker, look)))
    return actions


def _titled(
    title: str, *items: CardElement, element_id: str | None = None, hidden: bool = False
) -> Container:
    """A section with a bold title, set apart from the one above."""
    label = TextBlock(text=title, weight="Bolder", wrap=True)
    return _block(label, *items, element_id=element_id, hidden=hidden)


def spender_name(row: MemberRow) -> str:
    """The row's name as plain card text, or `Name unavailable` when none is known."""
    name = plain_name(row.display_name) if row.display_name else ""
    return name or NAME_UNAVAILABLE


def _spenders(state: BillingPanelState) -> Container:
    """`Top spenders` by name, then a grey `+ N more`."""
    rows = [
        spender_line(rank, spender_name(row), cost=row.cost_usd, is_caller=row.is_caller)
        for rank, row in enumerate(state.member_rows[:TOP_SPENDERS_SHOWN], start=1)
    ] or [NOTHING_USED]
    items: list[CardElement] = _rows(*rows)
    if overflow := more_spenders(len(state.member_rows), state.over_cap_count):
        items += _details(f"+ {overflow} more")
    return _titled(TOP_SPENDERS, *items)


def _channel_label(channel_id: str, names: Mapping[str, str]) -> str:
    """The channel's name as plain card text; never its id."""
    name = plain_name(names.get(channel_id) or "")
    return name or CHANNEL_NAME_UNAVAILABLE


def _channel_budgets(
    state: BillingPanelState, now: datetime, names: Mapping[str, str]
) -> Container:
    """`Channel budgets` by channel name, most used first, five then a grey `+ N more`."""
    lines = [
        channel_budget_line(status, label=_channel_label(status.budget.channel_id, names), now=now)
        for status in state.channel_budgets[:CHANNEL_BUDGETS_SHOWN]
    ]
    items: list[CardElement] = _rows(*lines)
    if more := more_channel_budgets(state):
        items += _details(f"+ {more} more")
    return _titled(CHANNEL_BUDGETS, *items)


def _rows(*lines: str) -> list[CardElement]:
    """List rows, close together."""
    return [TextBlock(text=line, spacing="Small", wrap=True) for line in lines]


def _panel_body(
    state: BillingPanelState,
    since: datetime,
    now: datetime,
    channel_names: Mapping[str, str],
    place: str | None,
    lookup: Lookup | None,
) -> list[CardElement]:
    subtext = (
        admin_summary(since, spend=state.guild_spend, people=state.guild_distinct_members)
        if state.is_admin
        else (month_label(since),)
    )
    body: list[CardElement] = [_header(TITLE, *subtext)]
    if not state.is_admin:
        own = caller_line(state.caller_spend, state.caller_cap, state.caller_turns)
        over = (
            "  ⚠️ Over your monthly limit"
            if spend_over_cap(state.caller_spend, state.caller_cap)
            else ""
        )
        body.append(_titled(f"{YOU}{over}", *_rows(own)))
    body.append(_block(*_credit(state)))
    if state.channel_budget is not None:
        phrase = channel_budget_phrase(state.channel_budget, now=now)
        body.append(_titled(CHANNEL_BUDGET, *_rows(phrase)))
    if state.is_admin:
        body.append(_spenders(state))
        if state.channel_budgets:
            body.append(_channel_budgets(state, now, channel_names))
    if state.timed_credit:
        rows = expiry_rows(state.timed_credit, when=card_date)
        body.append(_titled(EXPIRY_INTRO, *_rows(*rows), element_id=EXPIRY_ID, hidden=True))
    if actions := _actions(state, place):
        body.append(ActionSet(actions=actions, separator=True, spacing="Large"))
    if lookup is not None and state.is_admin:
        body.append(_titled(lookup.name, *_rows(lookup.line)))
    return body


def panel_card(
    state: BillingPanelState,
    *,
    since: datetime,
    now: datetime | None = None,
    notice: str | None = None,
    channel_names: Mapping[str, str] | None = None,
    place: str | None = None,
    lookup: Lookup | None = None,
) -> AdaptiveCard:
    """The member view, or for an admin the tenant view with its admin actions.

    ``channel_names`` names the channel budgets by id (`channel_labels`);
    ``place`` is the token every button carries for the channel it was asked in;
    ``lookup`` is an admin's last "Look up a person" pick, shown under the actions.
    """
    now = now or datetime.now(UTC)
    body = _panel_body(state, since, now, channel_names or {}, place, lookup)
    if notice is not None:
        body = [*text_lines(notice), *body]
    return AdaptiveCard(body=body, fallback_text=TITLE)


def _back(place: str | None = None) -> ExecuteAction:
    return button(VERB, "Back", "refresh", place=place or "")


def checkout_card(url: str, amount: int, place: str | None = None) -> AdaptiveCard:
    pay: list[Action] = [OpenUrlAction(title="Complete payment", url=url), _back(place)]
    body: list[CardElement] = [
        heading(f"💳 Top up ${amount}"),
        *text_lines("Pay in your browser.\n\nYour credit appears once the payment goes through."),
        ActionSet(actions=pay),
    ]
    return AdaptiveCard(body=body, fallback_text="Complete payment")


class BillingPanel:
    """Handlers for the command and the panel buttons."""

    def __init__(
        self,
        runtime: TeamsRuntime,
        *,
        roster_name: RosterName | None = None,
        team_channels: TeamChannels | None = None,
    ) -> None:
        self._runtime = runtime
        self._roster_name = roster_name
        self._team_channels = team_channels
        # Who typed the command -> token -> the channel it was typed in.
        self._places: dict[str, OrderedDict[str, str]] = {}

    async def command(self, context: CommandContext) -> None:
        asked, place = context.asked_in, None
        if asked is not None:
            place = secrets.token_urlsafe(16)
            held = self._places.setdefault(asked.user_id, OrderedDict())
            held[place] = asked.channel_id
            while len(held) > _MAX_PLACES:
                held.popitem(last=False)
        user_id = context.inbound.user_id
        card = await self._panel(context.tenant_id, user_id, context.is_admin, place=place)
        await context.send_card(card)

    async def on_action(
        self, ctx: ActivityContext[AdaptiveCardInvokeActivity]
    ) -> AdaptiveCardInvokeResponse:
        return await guarded(self._act(ctx.activity), toast(FAILED), "teams.billing.failed")

    def _channel_of(self, place: str | None, user_id: str) -> str | None:
        """The channel a held token names, for the person who typed the command only."""
        held = self._places.get(user_id)
        return held.get(place) if held is not None and place is not None else None

    async def _panel(
        self,
        tenant_id: uuid.UUID,
        user_id: str,
        is_admin: bool,
        *,
        notice: str | None = None,
        place: str | None = None,
        lookup: Lookup | None = None,
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
                channel_id=self._channel_of(place, user_id),
            )
            teams = await list_teams_installations(session, tenant_id=tenant_id)
            budget_ids = [
                s.budget.channel_id for s in state.channel_budgets[:CHANNEL_BUDGETS_SHOWN]
            ]
            stored_channels = await get_channel_names(
                session, tenant_id=tenant_id, platform="teams", channel_ids=budget_ids
            )
        team_ids = [team.team_id for team in teams]
        (state, channel_names) = await asyncio.gather(
            self._named(state, tenant_id, team_ids),
            self._channel_names(tenant_id, team_ids, budget_ids, stored_channels),
        )
        return panel_card(
            state,
            since=since,
            now=now,
            notice=notice,
            channel_names=channel_names,
            place=place,
            lookup=lookup,
        )

    async def _named(
        self, state: BillingPanelState, tenant_id: uuid.UUID, team_ids: list[str]
    ) -> BillingPanelState:
        """The state with the shown top spenders' roster names, else their stored names."""
        shown = state.member_rows[:TOP_SPENDERS_SHOWN]
        if not shown:
            return state
        stored = {row.platform_user_id: row.display_name for row in shown if row.display_name}
        labels, found = await roster_names(
            self._roster_name or _no_roster,
            team_ids=team_ids if self._roster_name is not None else [],
            user_ids=[row.platform_user_id for row in shown],
            stored=stored,
        )
        if found:
            remember_user_names(
                self._runtime.sessionmaker, tenant_id=tenant_id, platform="teams", names=found
            )
        rows: tuple[MemberRow, ...] = tuple(
            dataclasses.replace(row, display_name=labels.get(row.platform_user_id)) for row in shown
        )
        return dataclasses.replace(state, member_rows=rows + state.member_rows[TOP_SPENDERS_SHOWN:])

    async def _channel_names(
        self,
        tenant_id: uuid.UUID,
        team_ids: list[str],
        channel_ids: list[str],
        stored: dict[str, str],
    ) -> dict[str, str]:
        labels, listed = await channel_labels(
            self._team_channels, team_ids=team_ids, channel_ids=channel_ids, stored=stored
        )
        if listed:
            remember_channel_names(
                self._runtime.sessionmaker, tenant_id=tenant_id, platform="teams", names=listed
            )
        return labels

    async def _act(self, activity: AdaptiveCardInvokeActivity) -> AdaptiveCardInvokeResponse:
        actor = await card_actor(self._runtime, activity)
        if actor is None:
            return toast(DENIED)
        data = activity.value.action.data
        place = str(cast(object, data.get(PLACE)) or "") or None
        if data.get("op") == "redeem":
            code = str(cast(object, data.get(CODE_INPUT)) or "")
            return await self._redeem(actor, code, place)
        if data.get("op") == "lookup":
            return await self._lookup(actor, cast(object, data.get(PERSON_INPUT)), place)
        if data.get("op") != "topup":
            card = await self._panel(actor.tenant_id, actor.user_id, actor.is_admin, place=place)
            return replace_card(card)
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
            return replace_card(text_card("💸 Billing", NOT_CONFIGURED, back=_back(place)))
        return replace_card(checkout_card(url, amount, place))

    async def _lookup(
        self, actor: Actor, picked: object, place: str | None
    ) -> AdaptiveCardInvokeResponse:
        """One person's spend this month for a live admin, under the redrawn panel.

        Per-person spend is what a member's snapshot withholds, so admin is
        re-checked before any usage read.
        """
        if not actor.is_admin:
            return toast(LOOKUP_ADMIN_ONLY)
        user_id = canonical_uuid(picked)
        if user_id is None:
            return toast(PICK_A_PERSON)
        since = month_start(datetime.now(UTC))
        tenant_id = actor.tenant_id
        async with self._runtime.sessionmaker() as session:
            spend = await cost_for_user_in_tenant_since(
                session, tenant_id=tenant_id, platform_user_id=user_id, since=since
            )
            turns = await turn_count_for_user_in_tenant_since(
                session, tenant_id=tenant_id, platform_user_id=user_id, since=since
            )
            stored = await stored_name_labels(
                session, tenant_id=tenant_id, platform="teams", user_ids=[user_id]
            )
            teams = await list_teams_installations(session, tenant_id=tenant_id)
        labels, found = await roster_names(
            self._roster_name or _no_roster,
            team_ids=[team.team_id for team in teams] if self._roster_name is not None else [],
            user_ids=[user_id],
            stored=stored,
        )
        if found:
            remember_user_names(
                self._runtime.sessionmaker, tenant_id=tenant_id, platform="teams", names=found
            )
        name = plain_name(labels.get(user_id) or "") or NAME_UNAVAILABLE
        lookup = Lookup(name=name, line=lookup_line(spend, turns))
        card = await self._panel(tenant_id, actor.user_id, True, place=place, lookup=lookup)
        return replace_card(card)

    async def _redeem(
        self, actor: Actor, code: str, place: str | None
    ) -> AdaptiveCardInvokeResponse:
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
        card = await self._panel(
            actor.tenant_id, actor.user_id, True, notice=redeemed_text(result), place=place
        )
        return replace_card(card)
