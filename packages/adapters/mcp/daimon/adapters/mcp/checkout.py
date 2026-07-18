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
from daimon.core.billing import BillingConfig
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.auth import TokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

if TYPE_CHECKING:
    from stripe import StripeClient
    from stripe.params.checkout._session_create_params import SessionCreateParams

log = structlog.get_logger(__name__)


def build_checkout_route(
    *,
    stripe_client: StripeClient,
    billing_config: BillingConfig,
    auth: TokenVerifier,
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

        try:
            amount = int(body["amount"])
        except (KeyError, ValueError, TypeError):
            return Response(status_code=400)

        price_id = billing_config.prices.get(amount)
        if price_id is None:
            return Response(status_code=422)

        params: SessionCreateParams = {
            "mode": "payment",  # one-time, NOT subscription
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": billing_config.success_url,
            "cancel_url": billing_config.cancel_url,
            "metadata": {
                "tenant_id": str(tenant_id),  # the credit target
            },
        }
        session = await stripe_client.v1.checkout.sessions.create_async(params)
        log.info(
            "stripe.checkout.session_created",
            tenant_id=str(tenant_id),
            amount=amount,
            session_id=session.id,
        )
        return JSONResponse({"url": session.url})

    return handler


_SUCCESS_HTML = (
    "<html><body><h1>Payment received</h1>"
    "<p>Your server credit is updated. Return to Discord.</p>"
    "</body></html>"
)
_CANCEL_HTML = (
    "<html><body><h1>Checkout cancelled</h1>"
    "<p>No charge was made. Return to Discord.</p>"
    "</body></html>"
)


async def billing_success(_req: Request) -> HTMLResponse:
    return HTMLResponse(_SUCCESS_HTML)


async def billing_cancel(_req: Request) -> HTMLResponse:
    return HTMLResponse(_CANCEL_HTML)
