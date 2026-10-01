"""What the chat `billing` panels show, and the top-up hop to the MCP server.

A member gets only their own figures and the tenant balance: admin-only
figures are not read for a member rather than read and hidden. Top-ups POST
to the MCP server's `/billing/checkout`; chat adapters never import stripe.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import structlog
from daimon.core.billing import month_start as month_start
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
# Per-turn cost assumed while a tenant has no usage history yet.
_FALLBACK_TURN_COST_USD = 0.10


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


def member_label(user_id: str) -> str:
    """`User XXXX` from the id's last four characters; chat adapters keep no name cache."""
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
) -> BillingPanelState:
    """The caller's spend, turns and cap plus the balance; tenant totals only for an admin.

    ``now`` is when live timed promo credit is measured.
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
    )


def fmt_usd(value: float | Decimal) -> str:
    return f"${value:,.2f}"


def period_label(since: datetime) -> str:
    return f"Period: {since.strftime('%B %Y')} (UTC)"


def spend_over_cap(spend: float, cap: Decimal | None) -> bool:
    return cap is not None and spend > float(cap)


def caller_line(spend: float, cap: Decimal | None, turns: int) -> str:
    """The member view's own spend line, with the cap share when one applies."""
    if cap is None:
        return f"💸 {fmt_usd(spend)} spent · {turns} turns"
    cap_usd = float(cap)
    pct = int(spend / cap_usd * 100) if cap_usd > 0 else 0
    return f"💸 {fmt_usd(spend)} / {fmt_usd(cap_usd)} cap ({pct}%) · {turns} turns"


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
