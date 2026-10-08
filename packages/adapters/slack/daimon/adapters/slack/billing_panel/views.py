"""Pure Block Kit view builders for the Slack /billing panel.

Raw dicts only — no slack_sdk model types, no stripe, no I/O. All user-derived
strings are escaped with escape_mrkdwn (S5). The figures and formatters are
the chat-neutral ones in daimon.core.billing_panel.

These blocks only ever go into a modal (views.open / views.update), where a
`<@U…>` mention renders as the person's name and notifies nobody; that is how
top spenders are named. Never post them as a message: there a mention pings.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.billing_panel import (
    CHANNEL_BUDGETS_SHOWN,
    TOPUP_AMOUNTS,
    TOPUPS_ADMIN_ONLY,
    BillingPanelState,
    MemberRow,
    caller_line,
    channel_budget_line,
    channel_budget_phrase,
    estimate_turns,
    fmt_usd,
    more_channel_budgets,
    period_label,
    spend_over_cap,
    timed_credit_lines,
)

REDEEM_OPEN_ACTION_ID = "billing_redeem_open"
_TOP_SHOWN = 5
# A Slack user id, as a mention may carry it; anything else is shown as `User XXXX`.
_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")

# ---------------------------------------------------------------------------
# Block Kit builders (pure raw dicts — S4 pattern)
# ---------------------------------------------------------------------------


def slack_time(moment: datetime) -> str:
    """A moment every reader sees in their own timezone, with a UTC fallback."""
    fallback = f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    return f"<!date^{int(moment.timestamp())}^{{date_short_pretty}} {{time}}|{fallback}>"


def _context(text: str) -> dict[str, Any]:
    """Small grey detail lines under a group."""
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _server_credit_blocks(state: BillingPanelState) -> list[dict[str, Any]]:
    """The total balance, then the timed credit it includes in a grey context block."""
    blocks = [_section(f"🏦 *Server credit*\n*{fmt_usd(state.guild_balance_usd)}* total balance")]
    details = timed_credit_lines(state.timed_credit, when=slack_time)
    if not state.is_admin:
        details.append(TOPUPS_ADMIN_ONLY)
    if details:
        blocks.append(_context("\n".join(details)))
    return blocks


def _channel_budget_blocks(state: BillingPanelState, *, now: datetime) -> list[dict[str, Any]]:
    """The invoking channel's budget as its own group; nothing when it has none."""
    if state.channel_budget is None:
        return []
    phrase = channel_budget_phrase(state.channel_budget, now=now)
    return [_section("📊 *Channel budget*"), _context(phrase)]


def spender_name(row: MemberRow) -> str:
    """A mention of the person, which a modal shows as their name without notifying them.

    An id that is not a Slack user id falls back to the row's escaped `User XXXX` label.
    """
    if _USER_ID.fullmatch(row.platform_user_id):
        return f"<@{row.platform_user_id}>"
    return escape_mrkdwn(row.display_name)


def _channel_budgets_text(state: BillingPanelState) -> str | None:
    """The admin view's channel budgets, most used first; None when there are none."""
    if not state.channel_budgets:
        return None
    lines = ["📊 *Channel budgets*"] + [
        channel_budget_line(status, label=f"<#{status.budget.channel_id}>")
        for status in state.channel_budgets[:CHANNEL_BUDGETS_SHOWN]
    ]
    if more := more_channel_budgets(state):
        lines.append(f"_{more} more channel budgets_")
    return "\n".join(lines)


def build_loading_view() -> dict[str, Any]:
    """Return a Slack modal view dict showing a loading indicator."""
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Billing"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "⏳ Loading billing data…"},
            }
        ],
    }


def build_billing_container(
    state: BillingPanelState,
    *,
    now: datetime,
    since: datetime,
) -> list[dict[str, Any]]:
    """Build Block Kit blocks for the /billing modal view.

    Admin branch:
      - Header section: '💸 Billing · admin view' + period/workspace totals
      - Divider
      - Server credit section (total) + context (timed credit it includes)
      - Channel budget section + context, when the channel has one
      - Top spenders header + per-member rows (top 5 shown as mentions; overflow noted)
      - Channel budgets section, when any exist
      - Divider
      - Top-up actions block with static_select (admin only)

    Member branch:
      - Header section: '💸 Billing' + period
      - Divider
      - Caller section
      - Server credit section + context
      - Channel budget section + context, when the channel has one

    No color fields anywhere. User/agent-derived text is escaped via
    escape_mrkdwn (S5).
    """
    blocks: list[dict[str, Any]] = []

    if state.is_admin:
        subtext = (
            f"{period_label(since)} · "
            f"workspace total {fmt_usd(state.guild_spend)} · "
            f"{state.guild_turns} turns · "
            f"{state.guild_distinct_members} active members"
        )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*💸 Billing · admin view*\n{subtext}"},
            }
        )
        blocks.append({"type": "divider"})

        blocks += _server_credit_blocks(state)
        blocks += _channel_budget_blocks(state, now=now)

        # Top spenders
        top5 = state.member_rows[:_TOP_SHOWN]
        spenders_lines: list[str] = ["🏆 *Top spenders*"]
        if top5:
            for i, row in enumerate(top5):
                rank = i + 1
                you = " _(you)_" if row.is_caller else ""
                spend_str = fmt_usd(row.cost_usd)
                name = spender_name(row)
                spenders_lines.append(f"{rank}. {name}{you}  {spend_str} · {row.turn_count} turns")
        else:
            spenders_lines.append("no usage yet this period")

        overflow = max(0, len(state.member_rows) - _TOP_SHOWN) + state.over_cap_count
        if overflow > 0:
            spenders_lines.append(f"_{overflow} more members_")

        blocks.append(_section("\n".join(spenders_lines)))
        if (budgets_text := _channel_budgets_text(state)) is not None:
            blocks.append(_section(budgets_text))

        # Top-up static_select — admin only
        topup_options: list[dict[str, Any]] = []
        for amount in TOPUP_AMOUNTS:
            turns = estimate_turns(
                float(amount),
                guild_spend=state.guild_spend,
                guild_turns=state.guild_turns,
            )
            description_text = f"≈ {turns:,} turns"[:75]
            topup_options.append(
                {
                    "text": {"type": "plain_text", "text": f"${amount}"},
                    "value": str(amount),
                    "description": {"type": "plain_text", "text": description_text},
                }
            )
        elements: list[dict[str, Any]] = [
            {
                "type": "static_select",
                "action_id": "billing_topup",
                "placeholder": {"type": "plain_text", "text": "💳 Top up server credit…"},
                "options": topup_options,
            }
        ]
        if state.has_redeemable_promo_code:
            elements.append(
                {
                    "type": "button",
                    "action_id": REDEEM_OPEN_ACTION_ID,
                    "text": {"type": "plain_text", "text": "🎟️ Redeem code"},
                }
            )
        blocks.append({"type": "divider"})
        blocks.append({"type": "actions", "elements": elements})

    else:
        # Member (non-admin) branch
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*💸 Billing*\n{period_label(since)}",
                },
            }
        )
        blocks.append({"type": "divider"})

        # Caller spend
        if state.caller_spend == 0.0 and state.caller_turns == 0:
            caller_body = "no usage yet this period"
        else:
            caller_body = caller_line(state.caller_spend, state.caller_cap, state.caller_turns)
        over_cap = spend_over_cap(state.caller_spend, state.caller_cap)
        caller_header = "*You* ⚠️ over cap" if over_cap else "*You*"
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"{caller_header}\n{caller_body}"},
            }
        )

        blocks += _server_credit_blocks(state)
        blocks += _channel_budget_blocks(state, now=now)

    return blocks


def build_billing_view(
    state: BillingPanelState, *, now: datetime, since: datetime, channel_id: str | None = None
) -> dict[str, Any]:
    """The /billing modal around ``build_billing_container``.

    ``channel_id`` is the channel /billing ran in, kept in the view so a
    refresh from the redeem form still shows that channel's budget.
    """
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Billing"},
        "close": {"type": "plain_text", "text": "Close"},
        "private_metadata": json.dumps({"channel_id": channel_id or ""}),
        "blocks": build_billing_container(state, now=now, since=since),
    }
