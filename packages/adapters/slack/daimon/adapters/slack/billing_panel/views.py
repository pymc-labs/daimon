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
from daimon.adapters.slack.ui_render import mrkdwn_section, panel_blocks
from daimon.adapters.slack.web_button import open_in_web_button
from daimon.core.billing_panel import (
    ADD_CREDIT,
    EXPIRY_DATES,
    LOOK_UP,
    REDEEM_CODE,
    TITLE,
    TOPUP_AMOUNTS,
    BillingMarks,
    BillingPanelState,
    MemberRow,
    billing_screen,
    expiry_screen,
)
from daimon.core.promo_credit import ActiveTimedCredit
from daimon.core.slack_mrkdwn import escape_mrkdwn

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


def _button(text: str, action_id: str) -> dict[str, Any]:
    return {"type": "button", "action_id": action_id, "text": {"type": "plain_text", "text": text}}


def _modal(title: str, blocks: list[dict[str, Any]], *, close: str = "Close") -> dict[str, Any]:
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": close},
        "blocks": blocks,
    }


def person_name(user_id: str, name: str | None) -> str:
    """A person's name as escaped plain text on one line.

    Someone with no known name is a `<@U…>` mention, which a modal shows as their
    name without notifying them; an id that is not a Slack user id reads
    `That person`.
    """
    if name and (flat := " ".join(name.split())):
        return escape_mrkdwn(flat)
    return f"<@{user_id}>" if is_user_id(user_id) else UNKNOWN_PERSON


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
    return _modal(TITLE, [mrkdwn_section("Loading…")])


def spender_name(row: MemberRow) -> str:
    """The row's name, as `person_name` writes it."""
    return person_name(row.platform_user_id, row.display_name)


MARKS = BillingMarks(
    person=spender_name,
    channel=lambda channel_id: f"<#{channel_id}>",
    date=slack_date,
    you=" _(you)_",
)


def build_billing_container(
    state: BillingPanelState,
    *,
    now: datetime,
    since: datetime,
    lookup: Lookup | None = None,
    web_url: str | None = None,
) -> list[dict[str, Any]]:
    """The /billing modal's blocks: `billing_screen`, then the actions.

    `Add credit` and `Redeem code` (admin), `Expiry dates` with timed credit;
    for an admin a `Look up a person` picker, and under it ``lookup``, the
    picked person's name and spend.
    """
    blocks = panel_blocks(billing_screen(state, since=since, now=now, marks=MARKS))
    elements: list[dict[str, Any]] = []
    if state.is_admin:
        elements.append(_topup_select(state))
    if state.is_admin and state.has_redeemable_promo_code:
        elements.append(_button(REDEEM_CODE, REDEEM_OPEN_ACTION_ID))
    if state.timed_credit:
        elements.append(_button(EXPIRY_DATES, EXPIRY_OPEN_ACTION_ID))
    if web_url is not None:
        elements.append(open_in_web_button(web_url))
    if elements:
        blocks += [{"type": "divider"}, {"type": "actions", "elements": elements}]
    if state.is_admin:
        picker = {
            "type": "users_select",
            "action_id": LOOKUP_ACTION_ID,
            "placeholder": {"type": "plain_text", "text": LOOK_UP},
        }
        blocks.append({"type": "actions", "elements": [picker]})
        if lookup is not None:
            name = person_name(lookup.user_id, lookup.name)
            result = mrkdwn_section(f"*{name}*\n{lookup.line}")
            blocks.append(result | {"block_id": LOOKUP_BLOCK_ID})
    return blocks


def build_billing_view(
    state: BillingPanelState,
    *,
    now: datetime,
    since: datetime,
    channel_id: str | None = None,
    lookup: Lookup | None = None,
    web_url: str | None = None,
) -> dict[str, Any]:
    """The /billing modal around ``build_billing_container``.

    ``channel_id`` is the channel /billing ran in, kept in the view so a
    refresh from the redeem form or a lookup still shows that channel's budget.
    """
    blocks = build_billing_container(state, now=now, since=since, lookup=lookup, web_url=web_url)
    view = _modal(TITLE, blocks)
    view["private_metadata"] = json.dumps({"channel_id": channel_id or ""})
    return view


def build_expiry_view(credits: Sequence[ActiveTimedCredit]) -> dict[str, Any]:
    """Pushed by "Expiry dates": `Unused credit expires:` and `$20.00 on Oct 12` rows."""
    return _modal(EXPIRY_DATES, panel_blocks(expiry_screen(credits, marks=MARKS)), close="Back")
