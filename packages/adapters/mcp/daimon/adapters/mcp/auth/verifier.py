"""DaimonJWTVerifier — FastMCP JWTVerifier subclass that adds account-existence
guard inside `verify_token` and stashes `tenant_id` in claims.

Why here rather than in FastMCP middleware: `AuthorizationError` raised from
`Middleware.on_request` is silently swallowed by FastMCP's tool-list / call
dispatch paths (`except AuthorizationError: continue`). The only wire-level
401/403 producer is `RequireAuthMiddleware`, which runs during verification.
So the only way to turn "unknown account" into a real HTTP 401 is to return
`None` from `verify_token` — which this subclass does.

All three failure modes (bad sig, malformed/missing sub, unknown account)
collapse to HTTP 401.
"""

from __future__ import annotations

import uuid

from daimon.core.channel_admins import CHANNEL_ADMIN_PLATFORMS
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.channel_admins import has_channel_admin_grant
from daimon.core.stores.domain import Role
from daimon.core.stores.mcp_tokens import get_mcp_token
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class DaimonJWTVerifier(JWTVerifier):
    """HS256 verifier + account-existence guard.

    On success, stashes `tenant_id` (from the account's DB row) into
    `AccessToken.claims` so downstream middleware can read it without a
    second DB query, and `bound_channel_id` from an agent key's registry row
    when it was minted in a channel.
    """

    def __init__(
        self,
        *,
        secret: bytes,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        super().__init__(
            public_key=secret.decode(),
            algorithm="HS256",
        )
        self._sessionmaker = sessionmaker

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return None
        sub = access.claims.get("sub")
        if not isinstance(sub, str):
            return None
        try:
            account_id = uuid.UUID(sub)
        except ValueError:
            return None
        async with self._sessionmaker() as session:
            identity_row = await get_account_with_tenant(session, account_id=account_id)
            if identity_row is None:
                return None
            jti = access.claims.get("jti")
            if isinstance(jti, str):
                try:
                    jti_uuid = uuid.UUID(jti)
                except ValueError:
                    return None
                row = await get_mcp_token(session, jti=jti_uuid)
                if row is None or row.revoked_at is not None:
                    return None
                # A channel of another platform than the account's is no
                # binding this deployment minted.
                if row.platform is not None and row.platform != identity_row.platform:
                    return None
                bound_channel_id = row.channel_id
            else:
                bound_channel_id = None
            # Only the registry row binds a token to a channel, never a claim.
            access.claims.pop("bound_channel_id", None)
            if bound_channel_id is not None:
                access.claims["bound_channel_id"] = bound_channel_id
            access.claims["tenant_id"] = str(identity_row.tenant_id)
            access.claims["role"] = identity_row.role.value
            access.claims["platform"] = identity_row.platform
            access.claims["external_id"] = identity_row.external_id
            if identity_row.platform_user_id is not None:
                access.claims["platform_user_id"] = identity_row.platform_user_id
            # Always overwritten, so a token can never carry its own grant.
            access.claims["platform_role_ids"] = list(identity_row.platform_role_ids)
            access.claims["channel_admin"] = (
                identity_row.role is not Role.ADMIN
                and identity_row.platform in CHANNEL_ADMIN_PLATFORMS
                and identity_row.platform_user_id is not None
                and await has_channel_admin_grant(
                    session,
                    tenant_id=identity_row.tenant_id,
                    platform=identity_row.platform,
                    platform_user_id=identity_row.platform_user_id,
                    role_ids=identity_row.platform_role_ids,
                )
            )
        return access
