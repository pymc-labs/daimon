"""Typed accessor for per-request AuthIdentity set by IdentityMiddleware."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.billing import BillingConfig, is_over_cap
from daimon.core.tenant_balance import is_over_balance
from fastmcp import Context
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def _auth(ctx: Context) -> AuthIdentity:  # pyright: ignore[reportUnusedFunction]
    """Return the AuthIdentity seeded into request state by IdentityMiddleware.

    Raises ToolError if the state is missing — this is a programming error
    (middleware failed to run), not a caller-facing condition.
    """
    identity = await ctx.get_state("auth")
    if not isinstance(identity, AuthIdentity):
        raise ToolError("internal: missing auth context")
    return identity


def _require_admin(auth: AuthIdentity) -> None:  # pyright: ignore[reportUnusedFunction]
    """Raise ToolError if the caller is not an admin.

    Call at the top of every mutating _*_impl to enforce admin chat gating.
    Reads (list_*/get_*/self_read*/self_list*) stay ungated.
    """
    if not auth.is_admin:
        raise ToolError(
            "Changing my setup needs Manage Server — ask a server admin to use /agent-setup"
        )


async def _admit(  # pyright: ignore[reportUnusedFunction]
    auth: AuthIdentity,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    tool_name: str,
) -> AuthIdentity:
    """Balance and cap gate for an already-resolved identity.

    ``_check_admission`` wraps this for tools whose identity is the request's
    own; the hub mounts call it directly because their identity is chosen per
    call from the daimon being addressed.

    - ``platform_user_id is None`` is the trusted, fully-unbilled path:
      CLI-only/internal operator tokens run with no balance/cap checks, no
      usage row, no debit. This is intentional, not an oversight — never add
      a fallback that bills this path.
    - Otherwise runs ``is_over_balance`` then ``is_over_cap``; either denial
      raises a ``TERMINAL ERROR:`` ``ToolError`` naming ``/billing`` and logs
      a deny event carrying only ids (tenant/user/tool/gate) — never prompt
      content or raw Gemini text (Pitfall 9).
    """
    if auth.platform_user_id is None:
        return auth

    if await is_over_balance(sessionmaker=sessionmaker, tenant_id=auth.tenant_id):
        log.info(
            "mcp.admission_denied",
            tenant_id=str(auth.tenant_id),
            platform_user_id=auth.platform_user_id,
            tool=tool_name,
            gate="balance",
        )
        raise ToolError(
            "TERMINAL ERROR: This server's daimon credit is depleted. "
            "An admin can top up with /billing."
        )

    if await is_over_cap(
        billing_config=billing_config,
        sessionmaker=sessionmaker,
        tenant_id=auth.tenant_id,
        user_id=auth.platform_user_id,
        now=datetime.now(UTC),
    ):
        log.info(
            "mcp.admission_denied",
            tenant_id=str(auth.tenant_id),
            platform_user_id=auth.platform_user_id,
            tool=tool_name,
            gate="cap",
        )
        raise ToolError(
            "TERMINAL ERROR: Monthly usage cap reached for this guild. "
            "An admin can adjust the cap with /billing."
        )

    return auth


async def _check_admission(  # pyright: ignore[reportUnusedFunction]
    ctx: Context,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    tool_name: str,
) -> AuthIdentity:
    """Shared admission gate for the billed media and agent-chat turn tools. See ``_admit``."""
    return await _admit(
        await _auth(ctx),
        sessionmaker=sessionmaker,
        billing_config=billing_config,
        tool_name=tool_name,
    )
