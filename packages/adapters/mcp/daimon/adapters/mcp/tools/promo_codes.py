"""Promo code tools: redeem an operator-issued code for the caller's server or workspace.

``register_promo_code_tools(mcp, runtime)`` wires the ``@mcp.tool`` closure; it
delegates to ``_redeem_promo_code_impl`` so the logic is testable without a
FastMCP Context. Redemption itself lives in ``daimon.core.promo_credit``.
Every code needs a server admin; a channel budget code is also held to
`authorize` (SET_CHANNEL_BUDGET), which never allows a channel admin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import resolve_channel
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core import promo_credit
from daimon.core.authz import Action
from daimon.core.promo_codes import PromoRefusal, describe_refusal
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.domain import PromoCodeKind
from fastmcp import Context, FastMCP


@dataclass(frozen=True)
class RedeemPromoCodeResult:
    """Result returned from redeem_promo_code."""

    redeemed: bool
    refusal: PromoRefusal | None
    """Why the code was refused, when ``redeemed`` is False."""
    kind: PromoCodeKind | None
    """'credit' stays until spent; 'timed' only exists between its start and end;
    'channel_budget' raised a channel's budget limit instead of the balance."""
    amount_usd: str | None
    credit_starts_at: datetime | None
    credit_ends_at: datetime | None
    """When unspent timed credit expires."""
    balance_usd: str | None
    """The balance right after redeeming; timed credit that starts later is not in it yet."""
    channel_id: str | None
    """The channel whose budget a channel_budget code raised."""
    channel_limit_usd: str | None
    """That channel's budget limit after the raise."""
    message: str
    """One sentence to relay to the caller."""


_PLATFORMS = ("discord", "slack", "teams")


def _when(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


async def _redeem_promo_code_impl(
    runtime: McpRuntime, auth: AuthIdentity, code: str, channel_id: str | None = None
) -> RedeemPromoCodeResult:
    require_scope(auth, "promo:redeem")
    _require_admin(auth)
    channel = None
    if channel_id is not None and channel_id.strip() and auth.platform in _PLATFORMS:
        target = await resolve_channel(runtime, auth, channel_id.strip())
        channel = promo_credit.BudgetChannel(
            platform=str(auth.platform), channel_id=target.channel_id
        )
    result = await promo_credit.redeem_promo_code(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        code=code,
        now=datetime.now(UTC),
        channel=channel,
        redeemer=mcp_subject(auth, is_admin=auth.is_admin),
    )
    if isinstance(result, promo_credit.PromoRedeemRefused):
        if result.reason == "not_allowed":
            record_authz_denial(Action.SET_CHANNEL_BUDGET, "admin_required")
        return RedeemPromoCodeResult(
            redeemed=False,
            refusal=result.reason,
            kind=None,
            amount_usd=None,
            credit_starts_at=None,
            credit_ends_at=None,
            balance_usd=None,
            channel_id=None,
            channel_limit_usd=None,
            message=describe_refusal(result.reason),
        )
    amount = f"${result.amount_usd:.2f}"
    if result.channel_limit_usd is not None:
        message = (
            f"Raised channel {result.channel_id}'s budget by {amount}, "
            f"to ${result.channel_limit_usd:.2f}."
        )
    elif result.credit_ends_at is None:
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
        channel_id=result.channel_id,
        channel_limit_usd=(
            f"{result.channel_limit_usd:.2f}" if result.channel_limit_usd is not None else None
        ),
        message=message,
    )


def register_promo_code_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("promo:redeem")})
    async def redeem_promo_code(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        code: str,
        channel_id: str | None = None,
    ) -> RedeemPromoCodeResult:
        """Redeem a promo code for credit on this server or workspace.
        For example, apply the code someone was given to add credit to the balance.

        Case, spaces and dashes in ``code`` do not matter. Each code works once
        per server or workspace. Timed credit only exists between its start and
        end and is spent before other credit; whatever is left expires at the
        end. Repeated wrong codes pause redemption for a few minutes. Requires
        Manage Server (Discord) or workspace admin (Slack). A channel budget
        code raises one channel's budget limit instead: pass that channel's
        ``channel_id`` (the id from <channel role="parent_channel">).
        """
        return await _redeem_promo_code_impl(runtime, await _auth(ctx), code, channel_id)
