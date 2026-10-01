"""Pure Block Kit view builders for the Slack /billing panel.

Raw dicts only — no slack_sdk model types, no stripe, no I/O. All user-derived
strings are escaped with escape_mrkdwn (S5). The figures and formatters are
the chat-neutral ones in daimon.core.billing_panel.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.core.billing_panel import (
    TOPUP_AMOUNTS,
    BillingPanelState,
    caller_line,
    estimate_turns,
    fmt_usd,
    period_label,
    spend_over_cap,
)

REDEEM_OPEN_ACTION_ID = "billing_redeem_open"

# ---------------------------------------------------------------------------
# Block Kit builders (pure raw dicts — S4 pattern)
# ---------------------------------------------------------------------------


def slack_time(moment: datetime) -> str:
    """A moment every reader sees in their own timezone, with a UTC fallback."""
    fallback = f"{moment.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
    return f"<!date^{int(moment.timestamp())}^{{date_short_pretty}} {{time}}|{fallback}>"


def _timed_credit_lines(state: BillingPanelState) -> str:
    """One line per live timed promo credit, prefixed with a newline; empty when none."""
    lines = [
        f"\n⏳ {fmt_usd(c.remaining_usd)} timed credit left · ends {slack_time(c.ends_at)}"
        for c in state.timed_credit[:3]
    ]
    if len(state.timed_credit) > 3:
        lines.append(f"\n⏳ {len(state.timed_credit) - 3} more timed credits")
    return "".join(lines)


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
      - Server credit section
      - Top spenders header + per-member rows (top 5 shown; overflow noted)
      - Divider
      - Top-up actions block with static_select (admin only)

    Member branch:
      - Header section: '💸 Billing' + period
      - Divider
      - Caller section
      - Server credit section

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

        # Server credit
        credit_line = (
            f"🏦 *Server credit*\n{fmt_usd(state.guild_balance_usd)} balance"
            f"{_timed_credit_lines(state)}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": credit_line}})

        # Top spenders
        top5 = state.member_rows[:5]
        spenders_lines: list[str] = ["🏆 *Top spenders*"]
        if top5:
            for i, row in enumerate(top5):
                rank = i + 1
                you = " _(you)_" if row.is_caller else ""
                spend_str = fmt_usd(row.cost_usd)
                name = escape_mrkdwn(row.display_name)
                spenders_lines.append(f"{rank}. {name}{you}  {spend_str} · {row.turn_count} turns")
        else:
            spenders_lines.append("no usage yet this period")

        overflow = max(0, len(state.member_rows) - 5) + state.over_cap_count
        if overflow > 0:
            spenders_lines.append(f"_{overflow} more members_")

        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(spenders_lines)},
            }
        )

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

        # Server credit (top-ups are admin-only)
        credit_line = (
            f"🏦 *Server credit*\n"
            f"{fmt_usd(state.guild_balance_usd)} balance _(top-ups are admin-only)_"
            f"{_timed_credit_lines(state)}"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": credit_line}})

    return blocks


def build_billing_view(
    state: BillingPanelState, *, now: datetime, since: datetime
) -> dict[str, Any]:
    """The /billing modal around ``build_billing_container``."""
    return {
        "type": "modal",
        "title": {"type": "plain_text", "text": "Billing"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": build_billing_container(state, now=now, since=since),
    }
