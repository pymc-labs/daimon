"""POST to the MCP /billing/checkout route via an authenticated internal token.

Mirror of `daimon.adapters.discord.billing_panel.panel._create_checkout` — ported
to a standalone injectable function so tests can supply a transport-level fake.

NEVER import stripe here — Checkout creation lives in the MCP adapter only.
The Slack adapter calls /billing/checkout over HTTP with a bearer token; the
MCP route handles Stripe interaction (TOPUP-02 / anti-pattern from RESEARCH).

Tenant attribution (OQ-1): mint the token with the Slack principal's account_id
for the CURRENT workspace. The MCP verifier (DaimonJWTVerifier) re-derives the
tenant_id from the account row in the DB and populates it into AccessToken.claims.
The request body carries only {"amount": N} — no tenant_id/guild_id in the body.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
from daimon.core.config import McpSettings
from daimon.core.mcp_auth import mint_internal_mcp_token


async def create_checkout(
    http_client: httpx.AsyncClient,
    *,
    settings: McpSettings,
    account_id: uuid.UUID,
    amount: int,
) -> str:
    """POST to /billing/checkout and return the Stripe Checkout URL.

    Args:
        http_client:  Injected ``AsyncClient`` (caller creates/closes it).
        settings:     ``McpSettings`` with ``app_root_url`` and ``jwt_secret``.
        account_id:   The Slack principal's daimon ``account_id`` for the CURRENT
                      workspace — used as the ``sub`` claim so the MCP verifier can
                      look up the tenant (OQ-1).
        amount:       Integer top-up amount in USD (must match a configured price).

    Returns:
        The Stripe Checkout Session URL to embed in the ephemeral reply.

    Raises:
        AssertionError: if ``app_root_url`` or ``jwt_secret`` is not configured.
        httpx.HTTPStatusError: on non-2xx response from the MCP /billing/checkout route.
    """
    app_root_url = settings.app_root_url
    jwt_secret = settings.jwt_secret
    assert app_root_url is not None and jwt_secret is not None, (
        "MCP public_url + jwt_secret required for top-up; "
        "check DAIMON_MCP__PUBLIC_URL / DAIMON_MCP__JWT_SECRET"
    )
    token = mint_internal_mcp_token(
        account_id=account_id,
        secret=jwt_secret.get_secret_value().encode(),
        now=datetime.now(UTC),
    )
    resp = await http_client.post(
        f"{app_root_url.rstrip('/')}/billing/checkout",
        json={"amount": amount},  # tenant NOT in body — verifier derives from sub (OQ-1)
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return str(resp.json()["url"])
