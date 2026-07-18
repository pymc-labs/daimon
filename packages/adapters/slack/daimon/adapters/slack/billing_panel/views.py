"""Pure Block Kit view builders for the Slack /billing panel.

Raw dicts only — no slack_sdk model types, no stripe, no I/O. All user-derived
strings are escaped with escape_mrkdwn (S5). Imports only stdlib + sibling
modules (mrkdwn, state).

Ported from daimon.adapters.discord.billing_panel.panel:52-229 — pure
formatters and container builder adapted from Discord UI to Slack Block Kit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from daimon.adapters.slack.billing_panel.state import BillingPanelState
from daimon.adapters.slack.mrkdwn import escape_mrkdwn

# Fallback cost per turn used when workspace has no usage history yet.
_FALLBACK_TURN_COST_USD = 0.10


# ---------------------------------------------------------------------------
# Pure formatters (ported verbatim from billing_panel/panel.py:52-90)
# ---------------------------------------------------------------------------


def _fmt_usd(value: float | Decimal) -> str:
    return f"${value:,.2f}"


def _period_label(since: datetime) -> str:
    return f"Period: {since.strftime('%B %Y')} (UTC)"


def _is_over_cap(spend: float, cap: Decimal | None) -> bool:
    return cap is not None and spend > float(cap)


def _format_caller_line(spend: float, cap: Decimal | None, turns: int) -> str:
    """Regular-view caller spend line."""
    if cap is None:
        return f"💸 {_fmt_usd(spend)} spent · {turns} turns"
    cap_f = float(cap)
    pct = int(spend / cap_f * 100) if cap_f > 0 else 0
    return f"💸 {_fmt_usd(spend)} / {_fmt_usd(cap_f)} cap ({pct}%) · {turns} turns"


def estimate_turns(
    amount_usd: float,
    *,
    guild_spend: float,
    guild_turns: int,
) -> int:
    """Estimate turns purchasable for amount_usd given workspace usage history.

    Uses the workspace's average cost per turn when history is available
    (guild_spend > 0 and guild_turns > 0); falls back to
    _FALLBACK_TURN_COST_USD = $0.10/turn when there is no usage history yet.
    """
    if guild_spend > 0 and guild_turns > 0:
        cost_per_turn = guild_spend / guild_turns
    else:
        cost_per_turn = _FALLBACK_TURN_COST_USD
    return int(amount_usd / cost_per_turn)


# ---------------------------------------------------------------------------
# Block Kit builders (pure raw dicts — S4 pattern)
# ---------------------------------------------------------------------------


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
            f"{_period_label(since)} · "
            f"workspace total {_fmt_usd(state.guild_spend)} · "
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
        credit_line = f"🏦 *Server credit*\n{_fmt_usd(state.guild_balance_usd)} balance"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": credit_line}})

        # Top spenders
        top5 = state.member_rows[:5]
        spenders_lines: list[str] = ["🏆 *Top spenders*"]
        if top5:
            for i, row in enumerate(top5):
                rank = i + 1
                you = " _(you)_" if row.is_caller else ""
                spend_str = _fmt_usd(row.cost_usd)
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
        for amount in (10, 25, 50, 100):
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
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "static_select",
                        "action_id": "billing_topup",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "💳 Top up server credit…",
                        },
                        "options": topup_options,
                    }
                ],
            }
        )

    else:
        # Member (non-admin) branch
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*💸 Billing*\n{_period_label(since)}",
                },
            }
        )
        blocks.append({"type": "divider"})

        # Caller spend
        if state.caller_spend == 0.0 and state.caller_turns == 0:
            caller_body = "no usage yet this period"
        else:
            caller_body = _format_caller_line(
                state.caller_spend, state.caller_cap, state.caller_turns
            )
        over_cap = _is_over_cap(state.caller_spend, state.caller_cap)
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
            f"{_fmt_usd(state.guild_balance_usd)} balance _(top-ups are admin-only)_"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": credit_line}})

    return blocks
