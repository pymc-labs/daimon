"""Pure Block Kit view builders for the Slack /billing panel and the views it pushes.

Raw dicts only — no slack_sdk model types, no stripe, no I/O. All user-derived
strings are escaped with escape_mrkdwn (S5). The figures and words are the
chat-neutral ones in daimon.core.billing_panel.

The panel is a modal: the month, the credit left, the channel's budget, for an
admin the top spenders and channel budgets, and the actions, set apart by
dividers. "Expiry dates" pushes a view of its own over it. People are named in
plain escaped text (`names.py` resolves them). Only someone Slack never named
to us is shown as a `<@U…>` mention, which a modal renders as their name and
notifies nobody. These blocks only ever go into a modal (views.open,
views.update, views.push); never post them as a message, where a mention pings.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from daimon.adapters.slack.billing_panel.names import is_user_id
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
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
    credit_headline,
    expiry_rows,
    month_label,
    more_channel_budgets,
    more_spenders,
    spend_over_cap,
    spender_line,
    timed_credit_note,
)
from daimon.core.promo_credit import ActiveTimedCredit

TOPUP_ACTION_ID = "billing_topup"
REDEEM_OPEN_ACTION_ID = "billing_redeem_open"
EXPIRY_OPEN_ACTION_ID = "billing_expiry_open"
LOOKUP_ACTION_ID = "billing_lookup"
# The panel's own actions besides the top-up select and the redeem form.
PANEL_ACTION_IDS = frozenset({EXPIRY_OPEN_ACTION_ID, LOOKUP_ACTION_ID})
LOOKUP_BLOCK_ID = "billing_lookup_result"
UNKNOWN_PERSON = "That person"


@dataclasses.dataclass(frozen=True)
class Lookup:
    """A "Look up a person" pick: who, their name if known, and their spend line."""

    user_id: str
    name: str | None
    line: str


# ---------------------------------------------------------------------------
# Block Kit builders (pure raw dicts — S4 pattern)
# ---------------------------------------------------------------------------


def slack_time(moment: datetime) -> str:
    """A moment every reader sees in their own timezone, with a UTC fallback."""
    fallback = f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    return f"<!date^{int(moment.timestamp())}^{{date_short_pretty}} {{time}}|{fallback}>"


def slack_date(moment: datetime) -> str:
    """A date every reader sees in their own timezone, with a UTC fallback."""
    fallback = f"{moment.astimezone(UTC):%Y-%m-%d}"
    return f"<!date^{int(moment.timestamp())}^{{date_short_pretty}}|{fallback}>"


def _context(text: str) -> dict[str, Any]:
    """Small grey lines."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _divider() -> dict[str, Any]:
    return {"type": "divider"}


def _button(text: str, action_id: str) -> dict[str, Any]:
    return {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": text}}


def _modal(title: str, blocks: list[dict[str, Any]], *, close: str = "Close") -> dict[str, Any]:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": close},
        "blocks": blocks,
    }


def _credit_blocks(state: BillingPanelState) -> list[dict[str, Any]]:
    """`*$62.40* total credit left`, then the timed credit it includes in grey."""
    figure, words = credit_headline(state.guild_balance_usd)
    blocks = [_section(f"*{figure}*" + (f" {words}" if words else ""))]
    details = [note] if (note := timed_credit_note(state.timed_credit)) else []
    if not state.is_admin:
        details.append(ASK_ADMIN)
    if details:
        blocks.append(_context("\n".join(details)))
    return blocks


def person_name(user_id: str, name: str | None) -> str:
    """A person's name as escaped plain text on one line.

    Someone with no known name is a `<@U…>` mention, which a modal shows as their
    name without notifying them; an id that is not a Slack user id reads
    `That person`.
    """
    if name and (flat := " ".join(name.split())):
        return escape_mrkdwn(flat)
    return f"<@{user_id}>" if is_user_id(user_id) else UNKNOWN_PERSON


def spender_name(row: MemberRow) -> str:
    """The row's name, as `person_name` writes it."""
    return person_name(row.platform_user_id, row.display_name)


def _topup_select(state: BillingPanelState) -> dict[str, Any]:
    options: list[dict[str, Any]] = []
    for amount in TOPUP_AMOUNTS:
        options.append(
            {
                "text": {"type": "plain_text", "text": f"${amount}"},
                "value": str(amount),
            }
        )
    return {
        "type": "static_select",
        "action_id": TOPUP_ACTION_ID,
        "placeholder": {"type": "plain_text", "text": ADD_CREDIT},
        "options": options,
    }


def build_loading_view() -> dict[str, Any]:
    """Return a Slack modal view dict showing a loading indicator."""
    return _modal(TITLE, [_section("Loading…")])


def _spenders_blocks(state: BillingPanelState) -> list[dict[str, Any]]:
    """`*Top spenders*` by name, then a grey `+ N more`."""
    rows = [
        spender_line(
            rank, spender_name(row), cost=row.cost_usd, is_caller=row.is_caller, you=" _(you)_"
        )
        for rank, row in enumerate(state.member_rows[:TOP_SPENDERS_SHOWN], start=1)
    ] or [NOTHING_USED]
    blocks = [_section("\n".join([f"*{TOP_SPENDERS}*", *rows]))]
    if overflow := more_spenders(len(state.member_rows), state.over_cap_count):
        blocks.append(_context(f"+ {overflow} more"))
    return blocks


def _channel_budgets_blocks(state: BillingPanelState, now: datetime) -> list[dict[str, Any]]:
    """`*Channel budgets*`, most used first, five then a grey `+ N more`."""
    lines = [f"*{CHANNEL_BUDGETS}*"] + [
        channel_budget_line(status, label=f"<#{status.budget.channel_id}>", now=now)
        for status in state.channel_budgets[:CHANNEL_BUDGETS_SHOWN]
    ]
    blocks = [_section("\n".join(lines))]
    if more := more_channel_budgets(state):
        blocks.append(_context(f"+ {more} more"))
    return blocks


def build_billing_container(
    state: BillingPanelState,
    *,
    now: datetime,
    since: datetime,
    lookup: Lookup | None = None,
) -> list[dict[str, Any]]:
    """Block Kit blocks for the /billing modal, its sections set apart by dividers.

      - context: `October 2026` over `$48.17 spent by 9 people` (a member: the month)
      - a member's own use: `*You*` + `$11.50 of your $25.00 this month`
      - credit: `*$62.40* total credit left` + context with the timed credit it
        includes (and for a member `Ask an admin to add credit.`)
      - `*This channel*` + `$1.20 of $5.00 used this month`, when the
        invoking channel has one
      - admin only: `*Top spenders*` by name, then `*Channel budgets*`
      - actions: `Add credit` and `Redeem code` (admin), `Expiry dates` with
        timed credit; for an admin a `Look up a person` picker, and under it
        ``lookup``, the picked person's name and spend

    No color fields anywhere; the modal's own title is the panel's title.
    """
    subtext = (
        admin_summary(since, spend=state.guild_spend, people=state.guild_distinct_members)
        if state.is_admin
        else (month_label(since),)
    )
    blocks: list[dict[str, Any]] = [_context("\n".join(subtext)), _divider()]
    if not state.is_admin:
        own = caller_line(state.caller_spend, state.caller_cap, state.caller_turns)
        over = (
            "  ⚠️ Over your monthly limit"
            if spend_over_cap(state.caller_spend, state.caller_cap)
            else ""
        )
        blocks += [_section(f"*{YOU}*{over}\n{own}"), _divider()]
    blocks += _credit_blocks(state)
    if state.channel_budget is not None:
        phrase = channel_budget_phrase(state.channel_budget, now=now)
        blocks += [_divider(), _section(f"*{CHANNEL_BUDGET}*\n{phrase}")]
    if state.is_admin:
        blocks += [_divider(), *_spenders_blocks(state)]
        if state.channel_budgets:
            blocks += [_divider(), *_channel_budgets_blocks(state, now)]

    elements: list[dict[str, Any]] = []
    if state.is_admin:
        elements.append(_topup_select(state))
        if state.has_redeemable_promo_code:
            elements.append(_button(REDEEM_CODE, REDEEM_OPEN_ACTION_ID))
    if state.timed_credit:
        elements.append(_button(EXPIRY_DATES, EXPIRY_OPEN_ACTION_ID))
    if elements:
        blocks += [_divider(), {"type": "actions", "elements": elements}]
    if state.is_admin:
        picker = {
            "type": "users_select",
            "action_id": LOOKUP_ACTION_ID,
            "placeholder": {"type": "plain_text", "text": LOOK_UP},
        }
        blocks.append({"type": "actions", "elements": [picker]})
        if lookup is not None:
            name = person_name(lookup.user_id, lookup.name)
            result = _section(f"*{name}*\n{lookup.line}")
            blocks.append(result | {"block_id": LOOKUP_BLOCK_ID})
    return blocks


def build_billing_view(
    state: BillingPanelState,
    *,
    now: datetime,
    since: datetime,
    channel_id: str | None = None,
    lookup: Lookup | None = None,
) -> dict[str, Any]:
    """The /billing modal around ``build_billing_container``.

    ``channel_id`` is the channel /billing ran in, kept in the view so a
    refresh from the redeem form or a lookup still shows that channel's budget.
    """
    blocks = build_billing_container(state, now=now, since=since, lookup=lookup)
    view = _modal(TITLE, blocks)
    view["private_metadata"] = json.dumps({"channel_id": channel_id or ""})
    return view


def build_expiry_view(credits: Sequence[ActiveTimedCredit]) -> dict[str, Any]:
    """Pushed by "Expiry dates": `Unused credit expires:` and `$20.00 on Oct 12` rows."""
    rows = expiry_rows(credits, when=slack_date)
    return _modal(EXPIRY_DATES, [_section("\n".join([EXPIRY_INTRO, *rows]))], close="Back")
