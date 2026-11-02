"""Stripe Checkout Session creation route + success/cancel landing.

stripe is an MCP-only dependency — Checkout creation lives here, never in core or the
Discord adapter. StripeClient is constructed once at the edge (server.py) and injected;
no module-level singleton (guideline:architecture rule 3).

Per `guideline:architecture`: catch narrowly only at this HTTP boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog
from daimon.adapters.mcp.auth.verifier import CHAT_EXECUTION_CLAIM, TOKEN_KIND_CLAIM
from daimon.adapters.mcp.web_shell import render_page
from daimon.core.admin_actions import is_checkout_credential
from daimon.core.admin_authority import resolve_authority
from daimon.core.billing import BillingConfig, CheckoutAmountUnavailable, create_checkout_session
from daimon.core.config import Settings
from daimon.core.stores.accounts import get_account_with_tenant
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.auth import TokenVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

if TYPE_CHECKING:
    from stripe import StripeClient

log = structlog.get_logger(__name__)


def build_checkout_route(
    *,
    stripe_client: StripeClient,
    billing_config: BillingConfig,
    auth: TokenVerifier,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Callable[[Request], Awaitable[Response]]:
    """Construct the /billing/checkout POST handler with dependencies bound.

    Auth: verifies the Authorization: Bearer <token> header using the same
    TokenVerifier the MCP transport uses (injected — no module-level read).
    The success/cancel landing pages are public; only the checkout POST is gated.
    """

    async def handler(request: Request) -> Response:
        # --- bearer-token auth gate (D-WARNING 4) ---
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return Response(status_code=401)
        token = auth_header[len("bearer ") :]
        access: AccessToken | None = await auth.verify_token(token)
        if access is None:
            return Response(status_code=401)
        # Authority and money require a human-held user credential. Every
        # execution binding is refused, including an attended admin chat.
        if not is_checkout_credential(
            token_kind=access.claims.get(TOKEN_KIND_CLAIM),
            is_chat_execution=access.claims.get(CHAT_EXECUTION_CLAIM) is True,
            execution_bindings=tuple(
                access.claims.get(key)
                for key in (
                    "agent_id",
                    "chat_agent_id",
                    "routine_id",
                    "execution_id",
                    "chat_session_id",
                    "slack_turn_context_id",
                )
            ),
        ):
            return Response(status_code=403)

        # --- parse + validate request body ---
        try:
            body = await request.json()
        except Exception:
            return Response(status_code=400)

        # tenant_id sourced exclusively from the verified JWT claim.
        # A missing or non-str or non-UUID claim is a hard 403 (fail-closed).
        token_tenant_raw = access.claims.get("tenant_id")
        if not isinstance(token_tenant_raw, str):
            return Response(status_code=403)
        try:
            tenant_id = uuid.UUID(token_tenant_raw)
        except ValueError:
            return Response(status_code=403)

        subject = access.claims.get("sub")
        try:
            account_id = uuid.UUID(subject) if isinstance(subject, str) else None
        except ValueError:
            return Response(status_code=403)
        if account_id is None:
            return Response(status_code=403)
        async with sessionmaker() as session:
            identity = await get_account_with_tenant(session, account_id=account_id)
        if identity is None or identity.tenant_id != tenant_id or identity.platform_user_id is None:
            return Response(status_code=403)
        authority = await resolve_authority(
            sessionmaker, settings, tenant_id=tenant_id, platform_user_id=identity.platform_user_id
        )
        if authority.platform_admin is not True:
            return Response(status_code=403)

        try:
            amount = int(body["amount"])
        except (KeyError, ValueError, TypeError):
            return Response(status_code=400)

        try:
            url = await create_checkout_session(
                stripe_client, billing_config, tenant_id=tenant_id, amount=amount
            )
        except CheckoutAmountUnavailable:
            return Response(status_code=422)
        return JSONResponse({"url": url})

    return handler


async def billing_success(_req: Request) -> HTMLResponse:
    return render_page(
        title="Payment received",
        context="Billing",
        body_html="<h1>Payment received</h1><p>Your credit is updated. Return to your chat.</p>",
    )


async def billing_cancel(_req: Request) -> HTMLResponse:
    return render_page(
        title="Checkout cancelled",
        context="Billing",
        body_html="<h1>Checkout cancelled</h1><p>No charge was made. Return to your chat.</p>",
    )
