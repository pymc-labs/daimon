"""Deployment-wide promo issuance requires the human operator CLI.

An operator bearer does not establish human money authority, and a personal
tenant credential does not establish deployment-wide issuer scope. These
handlers require both and refuse MCP issuance without either proof.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_platform_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._scopes import require_operator_scope, scope_tags
from daimon.core.operator_tokens import issue_refusal
from daimon.core.promo_codes import (
    PromoCodeError,
    build_promo_code_terms,
    generate_promo_code,
    hash_promo_code,
    normalize_promo_code,
    parse_utc_timestamp,
)
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores.domain import PromoCodeKind, PromoCodeRow
from daimon.core.stores.mcp_tokens import add_issued_usd, lock_mcp_token
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_NOTHING = "Nothing was created."


@dataclass(frozen=True)
class PromoCodeSummary:
    """A promo code as stored; the code itself is never kept, only its hash."""

    promo_code_id: str
    kind: PromoCodeKind
    amount_usd: str
    credit_starts_at: str | None
    credit_ends_at: str | None
    redeem_starts_at: str | None
    redeem_ends_at: str | None
    max_redemptions: int | None
    redeemed_count: int
    note: str | None
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class CreatedPromoCode:
    promo_code_id: str
    code: str
    """Shown once: only a hash is stored."""
    kind: PromoCodeKind
    amount_usd: str
    credit_starts_at: str | None
    credit_ends_at: str | None
    redeem_starts_at: str | None
    redeem_ends_at: str | None
    max_redemptions: int | None
    note: str | None
    ceiling_remaining_usd: str | None
    """What this token may still issue, or null when it has no ceiling."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _summary(row: PromoCodeRow) -> PromoCodeSummary:
    return PromoCodeSummary(
        promo_code_id=str(row.id),
        kind=row.kind,
        amount_usd=f"{row.amount_usd:.2f}",
        credit_starts_at=_iso(row.credit_starts_at),
        credit_ends_at=_iso(row.credit_ends_at),
        redeem_starts_at=_iso(row.redeem_starts_at),
        redeem_ends_at=_iso(row.redeem_ends_at),
        max_redemptions=row.max_redemptions,
        redeemed_count=row.redeemed_count,
        note=row.note,
        created_at=row.created_at.isoformat(),
        revoked_at=_iso(row.revoked_at),
    )


def _instant(value: str | None) -> datetime | None:
    return parse_utc_timestamp(value) if value is not None and value.strip() else None


async def _create_promo_code_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    amount_usd: str,
    kind: PromoCodeKind,
    credit_starts_at: str | None = None,
    credit_ends_at: str | None = None,
    redeem_starts_at: str | None = None,
    redeem_ends_at: str | None = None,
    max_redemptions: int | None = None,
    note: str | None = None,
) -> CreatedPromoCode:
    """Validate, check the ceiling and insert in one transaction: a refusal leaves no code."""
    # Human money authority and deployment-wide issuer authority are distinct;
    # an operator bearer cannot substitute for direct human intent.
    _require_platform_admin(auth)
    require_operator_scope(auth, "promo:create")
    try:
        terms = build_promo_code_terms(
            amount_usd=Decimal(amount_usd.strip()),
            timed=kind == "timed",
            credit_starts_at=_instant(credit_starts_at),
            credit_ends_at=_instant(credit_ends_at),
            redeem_starts_at=_instant(redeem_starts_at),
            redeem_ends_at=_instant(redeem_ends_at),
            max_redemptions=max_redemptions,
            note=note,
        )
    except InvalidOperation as err:
        raise ToolError(f'amount_usd must be a decimal string such as "25.00". {_NOTHING}') from err
    except PromoCodeError as err:
        raise ToolError(f"{err}. {_NOTHING}") from err
    if auth.token_jti is None:
        raise ToolError("internal: operator identity without a token id")
    code = generate_promo_code()
    async with runtime.session_factory.begin() as session:
        token = await lock_mcp_token(session, jti=auth.token_jti)
        if token is None:
            raise ToolError(f"This operator token is no longer registered. {_NOTHING}")
        if token.revoked_at is not None:
            # Revoked after the verifier admitted this call; the lock orders us after it.
            raise ToolError(f"This operator token was revoked. {_NOTHING}")
        refusal = issue_refusal(
            max_issued_usd=token.max_issued_usd,
            issued_usd=token.issued_usd,
            amount_usd=terms.amount_usd,
            max_redemptions=terms.max_redemptions,
        )
        if refusal is not None:
            raise ToolError(f"{refusal}. {_NOTHING}")
        row = await promo_store.insert_promo_code(
            session, code_hash=hash_promo_code(normalize_promo_code(code)), terms=terms
        )
        if row is None:
            raise ToolError(f"The generated code collided with an existing one; retry. {_NOTHING}")
        remaining: Decimal | None = None
        if token.max_issued_usd is not None and terms.max_redemptions is not None:
            issued = terms.amount_usd * terms.max_redemptions
            await add_issued_usd(session, jti=token.jti, amount_usd=issued)
            remaining = token.max_issued_usd - token.issued_usd - issued
    return CreatedPromoCode(
        promo_code_id=str(row.id),
        code=code,
        kind=row.kind,
        amount_usd=f"{row.amount_usd:.2f}",
        credit_starts_at=_iso(row.credit_starts_at),
        credit_ends_at=_iso(row.credit_ends_at),
        redeem_starts_at=_iso(row.redeem_starts_at),
        redeem_ends_at=_iso(row.redeem_ends_at),
        max_redemptions=row.max_redemptions,
        note=row.note,
        ceiling_remaining_usd=f"{remaining:.2f}" if remaining is not None else None,
    )


async def _list_promo_codes_impl(runtime: McpRuntime, auth: AuthIdentity) -> list[PromoCodeSummary]:
    _require_platform_admin(auth)
    require_operator_scope(auth, "promo:create")
    async with runtime.session_factory() as session:
        rows = await promo_store.list_promo_codes(session)
    return [_summary(row) for row in rows]


async def _revoke_promo_code_impl(
    runtime: McpRuntime, auth: AuthIdentity, promo_code_id: str
) -> PromoCodeSummary:
    _require_platform_admin(auth)
    require_operator_scope(auth, "promo:create")
    try:
        code_id = uuid.UUID(promo_code_id.strip())
    except ValueError as err:
        raise ToolError(f"{promo_code_id!r} is not a promo code id") from err
    async with runtime.session_factory.begin() as session:
        row = await promo_store.revoke_promo_code(
            session, promo_code_id=code_id, now=datetime.now(UTC)
        )
    if row is None:
        raise ToolError(f"no promo code {code_id}")
    return _summary(row)


def register_promo_issuing_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    tags = scope_tags("promo:create")

    @mcp.tool(tags=tags)
    async def create_promo_code(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        amount_usd: str,
        kind: Literal["credit", "timed"],
        credit_starts_at: str | None = None,
        credit_ends_at: str | None = None,
        redeem_starts_at: str | None = None,
        redeem_ends_at: str | None = None,
        max_redemptions: int | None = None,
        note: str | None = None,
    ) -> CreatedPromoCode:
        """Unavailable over MCP: issue deployment-wide promos with the human operator CLI.

        ``amount_usd`` is the credit per redemption as a decimal string.
        ``credit`` codes stay until spent; ``timed`` codes need
        ``credit_starts_at`` and ``credit_ends_at`` and expire unspent credit
        at the end. Redemption is open between ``redeem_starts_at`` and
        ``redeem_ends_at`` when given (a timed code's defaults to its credit
        end). Dates are ISO 8601, UTC without an offset. ``code`` is shown
        only in this result.
        """
        return await _create_promo_code_impl(
            runtime,
            await _auth(ctx),
            amount_usd=amount_usd,
            kind=kind,
            credit_starts_at=credit_starts_at,
            credit_ends_at=credit_ends_at,
            redeem_starts_at=redeem_starts_at,
            redeem_ends_at=redeem_ends_at,
            max_redemptions=max_redemptions,
            note=note,
        )

    @mcp.tool(tags=tags)
    async def list_promo_codes(ctx: Context) -> list[PromoCodeSummary]:  # pyright: ignore[reportUnusedFunction]
        """Unavailable over MCP: list deployment-wide promos with the human operator CLI."""
        return await _list_promo_codes_impl(runtime, await _auth(ctx))

    @mcp.tool(tags=tags)
    async def revoke_promo_code(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, promo_code_id: str
    ) -> PromoCodeSummary:
        """Unavailable over MCP: revoke deployment-wide promos with the human operator CLI.

        Credit already redeemed stays, including timed credit that has not started.
        """
        return await _revoke_promo_code_impl(runtime, await _auth(ctx), promo_code_id)
