"""Request identity for the hub mounts.

Unlike ``AuthIdentity``, a hub caller is not one account: they are one
platform user who may act in several tenants. Tool code picks the tenant per
call (from the daimon being addressed) and only then builds an
``AuthIdentity`` for the existing agent-chat implementation functions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.hub_identity import HubTenant
from daimon.core.stores.domain import Platform
from fastmcp import Context
from fastmcp.exceptions import AuthorizationError, ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

HUB_AUTH_STATE_KEY = "hub_auth"


@dataclass(frozen=True, kw_only=True)
class HubIdentity:
    platform: Platform
    platform_user_id: str
    tenants: tuple[HubTenant, ...]

    def tenant(self, tenant_id: uuid.UUID) -> HubTenant | None:
        for t in self.tenants:
            if t.tenant_id == tenant_id:
                return t
        return None


class HubIdentityMiddleware(Middleware):
    """Decode the login claims into request state for the mount's platform.

    Both mounts sign with the same key, so a Slack login token verifies
    against the Discord proxy's signature check too; only the issuer and
    audience separate them. Comparing the claims' platform to the mount's is
    the second, independent layer behind that check.
    """

    def __init__(self, platform: Platform) -> None:
        self.platform = platform

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> object:
        from daimon.adapters.mcp.hub.claims import decode_hub_claims

        token = get_access_token()
        if token is None:
            raise AuthorizationError("missing access token")
        identity = decode_hub_claims(token.claims)
        if identity is None:
            raise AuthorizationError("token carries no hub identity")
        if identity.platform != self.platform:
            raise AuthorizationError("token was issued for another platform")
        fastmcp_ctx = context.fastmcp_context
        if fastmcp_ctx is None:
            raise AuthorizationError("missing fastmcp context on request")
        await fastmcp_ctx.set_state(HUB_AUTH_STATE_KEY, identity, serializable=False)
        return await call_next(context)


async def _hub_auth(ctx: Context) -> HubIdentity:  # pyright: ignore[reportUnusedFunction]
    identity = await ctx.get_state(HUB_AUTH_STATE_KEY)
    if not isinstance(identity, HubIdentity):
        raise ToolError("internal: missing hub auth context")
    return identity
