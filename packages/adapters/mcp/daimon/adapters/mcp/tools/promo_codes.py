"""Promo code tools: redeem an operator-issued code for the caller's server or workspace.

``register_promo_code_tools(mcp, runtime)`` wires the ``@mcp.tool`` closure; it
delegates to ``_redeem_promo_code_impl`` so the logic is testable without a
FastMCP Context. Redemption itself lives in ``daimon.core.promo_credit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import promo_credit
from daimon.core.promo_codes import PromoRefusal, describe_refusal
from daimon.core.stores.domain import PromoCodeKind
from fastmcp import Context, FastMCP


@dataclass(frozen=True)
class RedeemPromoCodeResult:
    """Result returned from redeem_promo_code."""

    redeemed: bool
    refusal: PromoRefusal | None
    """Why the code was refused, when ``redeemed`` is False."""
    kind: PromoCodeKind | None
    """'credit' stays until spent; 'timed' only exists between its start and end."""
    amount_usd: str | None
    credit_starts_at: datetime | None
    credit_ends_at: datetime | None
    """When unspent timed credit expires."""
    balance_usd: str | None
    """The balance right after redeeming; timed credit that starts later is not in it yet."""
    message: str
    """One sentence to relay to the caller."""


def _when(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


async def _redeem_promo_code_impl(
    runtime: McpRuntime, auth: AuthIdentity, code: str
) -> RedeemPromoCodeResult:
    _require_admin(auth)
    result = await promo_credit.redeem_promo_code(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        code=code,
        now=datetime.now(UTC),
    )
    if isinstance(result, promo_credit.PromoRedeemRefused):
        return RedeemPromoCodeResult(
            redeemed=False,
            refusal=result.reason,
            kind=None,
            amount_usd=None,
            credit_starts_at=None,
            credit_ends_at=None,
            balance_usd=None,
            message=describe_refusal(result.reason),
        )
    amount = f"${result.amount_usd:.2f}"
    if result.credit_ends_at is None:
        message = f"Redeemed {amount} of credit."
    elif result.granted or result.credit_starts_at is None:
        message = f"Redeemed {amount} of credit, usable until {_when(result.credit_ends_at)}."
    else:
        message = (
            f"Redeemed {amount} of credit, usable from {_when(result.credit_starts_at)} "
            f"until {_when(result.credit_ends_at)}."
        )
    return RedeemPromoCodeResult(
        redeemed=True,
        refusal=None,
        kind=result.kind,
        amount_usd=f"{result.amount_usd:.2f}",
        credit_starts_at=result.credit_starts_at,
        credit_ends_at=result.credit_ends_at,
        balance_usd=f"{result.balance_usd:.2f}",
        message=message,
    )


def register_promo_code_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def redeem_promo_code(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        code: str,
    ) -> RedeemPromoCodeResult:
        """Redeem a promo code for credit on this server or workspace.
        For example, apply the code someone was given to add credit to the balance.

        Case, spaces and dashes in ``code`` do not matter. Each code works once
        per server or workspace. Timed credit only exists between its start and
        end and is spent before other credit; whatever is left expires at the
        end. Repeated wrong codes pause redemption for a few minutes. Requires
        Manage Server (Discord) or workspace admin (Slack).
        """
        return await _redeem_promo_code_impl(runtime, await _auth(ctx), code)
