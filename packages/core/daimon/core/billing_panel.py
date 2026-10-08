"""What the chat `billing` panels show, and the top-up hop to the MCP server.

A member gets only their own figures and the tenant balance: admin-only
figures are not read for a member rather than read and hidden. Top-ups POST
to the MCP server's `/billing/checkout`; chat adapters never import stripe.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import httpx
import structlog
from daimon.core.billing import month_start as month_start
from daimon.core.channel_budget import (
    ChannelBudgetStatus,
    get_channel_budget_status,
    list_channel_budget_statuses,
    window_label,
)
from daimon.core.config import McpSettings
from daimon.core.errors import DaimonError
from daimon.core.mcp_auth import mint_jwt
from daimon.core.promo_credit import ActiveTimedCredit, get_active_timed_credit
from daimon.core.stores import tenant_user_caps
from daimon.core.stores.promo_codes import has_redeemable_promo_code
from daimon.core.stores.tenant_ledger import get_balance
from daimon.core.stores.usage_events import (
    cost_for_tenant_since,
    cost_for_user_in_tenant_since,
    costs_by_user_in_tenant_since,
    turn_count_for_tenant_since,
    turn_count_for_user_in_tenant_since,
    turns_by_user_in_tenant_since,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

# Any other amount is refused: each must match a configured Stripe price.
TOPUP_AMOUNTS = (10, 25, 50, 100)
MEMBER_CAP = 25
# Channel budgets the admin view lists, most used first.
CHANNEL_BUDGETS_SHOWN = 5
# Per-turn cost assumed while a tenant has no usage history yet.
_FALLBACK_TURN_COST_USD = 0.10
# Rows the admin panel names under Top spenders; the rest are counted.
TOP_SPENDERS_SHOWN = 5
# Timed credits "Expiry dates" lists, soonest first; the rest are counted.
EXPIRY_SHOWN = 5
# Timed credit ending within this long turns the panel's accent amber.
EXPIRY_WARNING = timedelta(days=7)

# The words every platform's panel uses, so they read the same everywhere.
TITLE = "Billing"
YOU = "You"
ASK_ADMIN = "Ask an admin to add credit."
NOTHING_USED = "Nothing used this month"
ADD_CREDIT = "Add credit"
REDEEM_CODE = "Redeem code"
EXPIRY_DATES = "Expiry dates"
EXPIRY_INTRO = "Unused credit expires:"
LOOK_UP = "Look up a person"
CHANNEL_BUDGET = "This channel"
TOP_SPENDERS = "Top spenders"
CHANNEL_BUDGETS = "Channel budgets"


@dataclasses.dataclass(frozen=True)
class MemberRow:
    platform_user_id: str
    display_name: str
    cost_usd: float
    turn_count: int
    is_caller: bool


@dataclasses.dataclass(frozen=True)
class BillingPanelState:
    """One viewer's snapshot. Tenant activity fields are zero or empty for a member."""

    is_admin: bool
    caller_user_id: str
    caller_spend: float
    caller_turns: int
    caller_cap: Decimal | None
    guild_balance_usd: Decimal  # SUM(tenant_ledger.delta_usd); negative = depleted
    guild_spend: float
    guild_turns: int
    guild_distinct_members: int
    member_rows: tuple[MemberRow, ...]  # sorted, capped at MEMBER_CAP
    over_cap_count: int  # spending members beyond MEMBER_CAP
    # Live timed promo credit (both views), soonest-ending first; empty without promo codes
    timed_credit: tuple[ActiveTimedCredit, ...] = ()
    # Some promo code is redeemable now (admin view only); gates the redeem button
    has_redeemable_promo_code: bool = False
    # The invoking channel's budget (both views); None when it has none.
    channel_budget: ChannelBudgetStatus | None = None
    # Every channel budget, by share of its limit spent (admin view only); empty for a member
    channel_budgets: tuple[ChannelBudgetStatus, ...] = ()


def member_label(user_id: str) -> str:
    """`User XXXX` from the id's last four characters: the fallback label for a person.

    Rows start with it, and each adapter replaces it on the rows it shows with
    the person's name where its platform can tell it (a Discord member fetch,
    a Slack user mention, a Teams team roster). Anyone it cannot resolve, such
    as someone who has left, keeps this label.
    """
    return f"User {user_id[-4:]}" if len(user_id) >= 4 else "<unknown user>"


async def _has_redeemable_promo_code_or_false(session: AsyncSession, *, now: datetime) -> bool:
    """Whether a promo code is redeemable now; False when the lookup fails.

    Named boundary: a failed lookup only hides the redeem button. It runs in a
    savepoint, so the rest of the snapshot still reads.
    """
    try:
        async with session.begin_nested():
            return await has_redeemable_promo_code(session, now=now)
    except SQLAlchemyError as exc:
        log.warning("billing_panel.promo_code_lookup_failed", error=str(exc))
        return False


async def load_billing_snapshot(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    is_admin: bool,
    since: datetime,
    now: datetime,
    platform: str | None = None,
    channel_id: str | None = None,
) -> BillingPanelState:
    """The caller's spend, turns and cap plus the balance; tenant totals only for an admin.

    ``now`` is when live timed promo credit and the budget window are measured.
    With ``platform`` and ``channel_id``, the state carries that channel's budget.
    An admin also gets every channel budget, most used first.
    """
    state = BillingPanelState(
        is_admin=False,
        caller_user_id=platform_user_id,
        caller_spend=await cost_for_user_in_tenant_since(
            session, platform_user_id=platform_user_id, tenant_id=tenant_id, since=since
        ),
        caller_turns=await turn_count_for_user_in_tenant_since(
            session, platform_user_id=platform_user_id, tenant_id=tenant_id, since=since
        ),
        caller_cap=await tenant_user_caps.get_effective_cap(
            session, tenant_id=tenant_id, user_id=platform_user_id
        ),
        guild_balance_usd=await get_balance(session, tenant_id=tenant_id),
        guild_spend=0.0,
        guild_turns=0,
        guild_distinct_members=0,
        member_rows=(),
        over_cap_count=0,
        timed_credit=tuple(await get_active_timed_credit(session, tenant_id=tenant_id, now=now)),
        channel_budget=(
            None
            if platform is None or channel_id is None
            else await get_channel_budget_status(
                session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
            )
        ),
    )
    if not is_admin:
        return state
    costs = await costs_by_user_in_tenant_since(session, tenant_id=tenant_id, since=since)
    turns = await turns_by_user_in_tenant_since(session, tenant_id=tenant_id, since=since)
    user_ids = set(costs) | set(turns)
    rows = sorted(
        (
            MemberRow(
                platform_user_id=user_id,
                display_name=member_label(user_id),
                cost_usd=costs.get(user_id, 0.0),
                turn_count=turns.get(user_id, 0),
                is_caller=user_id == platform_user_id,
            )
            for user_id in user_ids
        ),
        key=lambda row: (-row.cost_usd, row.platform_user_id),
    )
    return dataclasses.replace(
        state,
        is_admin=True,
        guild_spend=await cost_for_tenant_since(session, tenant_id=tenant_id, since=since),
        guild_turns=await turn_count_for_tenant_since(session, tenant_id=tenant_id, since=since),
        guild_distinct_members=len(user_ids),
        member_rows=tuple(rows[:MEMBER_CAP]),
        over_cap_count=max(0, len(rows) - MEMBER_CAP),
        has_redeemable_promo_code=await _has_redeemable_promo_code_or_false(session, now=now),
        channel_budgets=tuple(
            await list_channel_budget_statuses(
                session, tenant_id=tenant_id, platform=platform, now=now
            )
        ),
    )


def fmt_usd(value: float | Decimal) -> str:
    """`$1,234.50`; a negative amount reads `-$12.00`."""
    if value < 0:
        return f"-${-value:,.2f}"
    return f"${value:,.2f}"


def credit_headline(balance: Decimal) -> tuple[str, str | None]:
    """The big figure and the words under it: `$62.40`, `total credit left`.

    At or below zero the figure is `No credit left`, with `$3.10 spent beyond it`
    under it when the balance is negative (an operator-funded tenant).
    """
    if balance > 0:
        return fmt_usd(balance), "total credit left"
    return "No credit left", (f"{fmt_usd(-balance)} spent beyond it" if balance < 0 else None)


def timed_credit_note(credits: Sequence[ActiveTimedCredit]) -> str | None:
    """`Includes $25.00 that expires. It's used first.`; None without timed credit.

    The amount is everything left in live timed credit; when each part expires
    is behind the panel's "Expiry dates" action.
    """
    if not credits:
        return None
    amount = fmt_usd(sum((credit.remaining_usd for credit in credits), Decimal("0")))
    return f"Includes {amount} that expires. It's used first."


def expiry_rows(
    credits: Sequence[ActiveTimedCredit], *, when: Callable[[datetime], str]
) -> list[str]:
    """`$20.00 · Oct 12` per timed credit, soonest first, at most ``EXPIRY_SHOWN``, then `+ N more`.

    ``when`` renders a date in the platform's own syntax.
    """
    ordered = sorted(credits, key=lambda credit: credit.ends_at)
    rows = [
        f"{fmt_usd(credit.remaining_usd)} · {when(credit.ends_at)}"
        for credit in ordered[:EXPIRY_SHOWN]
    ]
    if (more := len(ordered) - EXPIRY_SHOWN) > 0:
        rows.append(f"+ {more} more")
    return rows


Tone = Literal["alert", "warning"]


def panel_tone(
    *,
    balance: Decimal,
    caller_spend: float,
    caller_cap: Decimal | None,
    credits: Sequence[ActiveTimedCredit],
    now: datetime,
) -> Tone | None:
    """The panel's state for an accent: `alert` with no credit left or the caller over
    their cap, `warning` when timed credit expires within ``EXPIRY_WARNING``, else None."""
    if balance <= 0 or spend_over_cap(caller_spend, caller_cap):
        return "alert"
    if any(credit.ends_at - now <= EXPIRY_WARNING for credit in credits):
        return "warning"
    return None


def channel_budget_phrase(status: ChannelBudgetStatus, *, now: datetime) -> str:
    """The invoking channel's budget in words, e.g. `$1.20 of $5.00 used this month`.

    A budget that has not started shows its limit and when it starts; a fixed
    one that has ended says so.
    """
    budget = status.budget
    spent = f"{fmt_usd(status.spent_usd)} of {fmt_usd(budget.limit_usd)} used"
    if budget.window == "monthly":
        return f"{spent} this month"
    if budget.starts_at is None:
        return f"{spent} in total"
    total = budget.window == "total" or budget.ends_at is None
    if budget.starts_at > now:
        when = window_label(budget, started=False)
        return f"{fmt_usd(budget.limit_usd)} budget {when if total else f'from {when}'}"
    if total:
        return f"{spent} {window_label(budget)}"
    ended = "" if status.is_active else " (ended)"
    return f"{spent} from {window_label(budget)}{ended}"


def channel_budget_line(status: ChannelBudgetStatus, *, label: str, now: datetime) -> str:
    """`#team-a  $4.00 of $5.00 used this month`, with the platform's channel ``label``."""
    return f"{label}  {channel_budget_phrase(status, now=now)}"


def more_channel_budgets(state: BillingPanelState) -> int:
    """Budgets past the first ``CHANNEL_BUDGETS_SHOWN``, for an "N more" line."""
    return max(0, len(state.channel_budgets) - CHANNEL_BUDGETS_SHOWN)


def month_label(since: datetime) -> str:
    """`October 2026`: the month the panel covers, and the member view's subtext."""
    return since.strftime("%B %Y")


def admin_summary(since: datetime, *, spend: float, people: int) -> str:
    """The admin view's subtext: `October 2026 · $48.17 spent by 9 people`."""
    if people == 0:
        return f"{month_label(since)} · nothing used yet"
    who = "1 person" if people == 1 else f"{people} people"
    return f"{month_label(since)} · {fmt_usd(spend)} spent by {who}"


def spend_over_cap(spend: float, cap: Decimal | None) -> bool:
    return cap is not None and spend > float(cap)


def caller_line(spend: float, cap: Decimal | None, turns: int) -> str:
    """The member's own use: `$11.50 of your $25.00 this month`, `$11.50 used this month`."""
    if not spend and not turns:
        return NOTHING_USED
    if cap is None:
        return f"{fmt_usd(spend)} used this month"
    return f"{fmt_usd(spend)} of your {fmt_usd(cap)} this month"


def lookup_line(spend: float, turns: int) -> str:
    """One person's use in the admin member lookup: `$14.02 this month`."""
    return NOTHING_USED if not spend and not turns else f"{fmt_usd(spend)} this month"


def spender_line(rank: int, name: str, *, cost: float, is_caller: bool, you: str = " (you)") -> str:
    """`1. Maya Chen  $14.02`; ``name`` is already in the platform's safe form."""
    return f"{rank}. {name}{you if is_caller else ''}  {fmt_usd(cost)}"


def more_spenders(row_count: int, over_cap_count: int) -> int:
    """Spenders past the first ``TOP_SPENDERS_SHOWN``, for a `+ N more` line."""
    return max(0, row_count - TOP_SPENDERS_SHOWN) + over_cap_count


def estimate_turns(amount_usd: float, *, guild_spend: float, guild_turns: int) -> int:
    """Turns `amount_usd` buys at the tenant's average turn cost, or $0.10 without history."""
    has_history = guild_spend > 0 and guild_turns > 0
    cost_per_turn = guild_spend / guild_turns if has_history else _FALLBACK_TURN_COST_USD
    return int(amount_usd / cost_per_turn)


async def create_checkout(
    http_client: httpx.AsyncClient, *, settings: McpSettings, account_id: uuid.UUID, amount: int
) -> str:
    """POST `/billing/checkout` and return the Stripe Checkout URL.

    The token's `sub` is the clicker's account in the current tenant; the MCP
    verifier derives the tenant from it, so the body carries only the amount.
    The route never checks admin, so the token is a plain account token: an
    internal admin one would be a non-revocable admin bearer for any account.
    Raises DaimonError when the MCP url or JWT secret is unset and
    httpx.HTTPStatusError on a non-2xx answer (no billing routes mounted is a 404).
    """
    app_root_url = settings.app_root_url
    jwt_secret = settings.jwt_secret
    if app_root_url is None or jwt_secret is None:
        raise DaimonError("Top-ups need DAIMON_MCP__PUBLIC_URL and DAIMON_MCP__JWT_SECRET.")
    token = mint_jwt(
        account_id=account_id,
        secret=jwt_secret.get_secret_value().encode(),
        now=datetime.now(UTC),
    )
    response = await http_client.post(
        f"{app_root_url.rstrip('/')}/billing/checkout",
        json={"amount": amount},
        headers={"Authorization": f"Bearer {token}"},
    )
    response.raise_for_status()
    return str(response.json()["url"])
