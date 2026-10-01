"""Operator token scopes and the promo issuing ceiling. Pure: no clock, no I/O.

An operator token lets an external integration call a fixed set of MCP
tools on behalf of one server admin. Each scope names a group of tools;
the MCP adapter tags each tool with ``scope:<name>`` and shows an operator
token only the tools of the scopes its row holds.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Literal, get_args

OperatorScope = Literal["tenant:read", "channels:write", "promo:redeem", "promo:create"]
"""``tenant:read`` reads the tenant summary and channel budgets;
``channels:write`` sets and clears per-channel settings; ``promo:redeem``
redeems a promo code for the tenant; ``promo:create`` issues, lists and
revokes promo codes for the whole deployment, so only the CLI mints it."""

OPERATOR_SCOPES: tuple[OperatorScope, ...] = get_args(OperatorScope)
DEPLOYMENT_SCOPES: frozenset[OperatorScope] = frozenset({"promo:create"})
"""Scopes that act beyond the token's tenant."""

MAX_TTL_DAYS = 365


class OperatorTokenError(ValueError):
    """Operator token terms an operator supplied are inconsistent."""


def scope_tag(scope: str) -> str:
    """The tool tag that grants ``scope``."""
    return f"scope:{scope}"


def parse_operator_scopes(raw: Iterable[str]) -> frozenset[OperatorScope]:
    """Validate requested scopes; at least one, each one known."""
    known: dict[str, OperatorScope] = {scope: scope for scope in OPERATOR_SCOPES}
    scopes: set[OperatorScope] = set()
    for value in raw:
        scope = known.get(value.strip())
        if scope is None:
            raise OperatorTokenError(
                f"unknown scope {value.strip()!r}; choose from {', '.join(OPERATOR_SCOPES)}"
            )
        scopes.add(scope)
    if not scopes:
        raise OperatorTokenError("an operator token needs at least one scope")
    return frozenset(scopes)


def validate_operator_terms(
    *, scopes: frozenset[OperatorScope], ttl_days: int, max_issued_usd: Decimal | None
) -> None:
    """Refuse a lifetime out of range, or a ceiling on a token that cannot issue."""
    if not 1 <= ttl_days <= MAX_TTL_DAYS:
        raise OperatorTokenError(f"ttl must be 1 to {MAX_TTL_DAYS} days")
    if max_issued_usd is None:
        return
    if "promo:create" not in scopes:
        raise OperatorTokenError("an issuing ceiling needs the promo:create scope")
    if not max_issued_usd.is_finite() or max_issued_usd <= 0:
        raise OperatorTokenError("the issuing ceiling must be a positive dollar amount")


def issue_refusal(
    *,
    max_issued_usd: Decimal | None,
    issued_usd: Decimal,
    amount_usd: Decimal,
    max_redemptions: int | None,
) -> str | None:
    """Why a token may not issue this code, or None when it may.

    A code can grant ``amount_usd`` to ``max_redemptions`` tenants, so that
    product is what it counts against the ceiling. A token with a ceiling
    cannot issue a code with unlimited redemptions.
    """
    if max_issued_usd is None:
        return None
    if max_redemptions is None:
        return "This token has an issuing ceiling, so max_redemptions is required"
    remaining = max_issued_usd - issued_usd
    if amount_usd * max_redemptions > remaining:
        return (
            f"This code could grant ${amount_usd * max_redemptions:.2f}, more than the "
            f"${max(remaining, Decimal(0)):.2f} left under this token's issuing ceiling"
        )
    return None
