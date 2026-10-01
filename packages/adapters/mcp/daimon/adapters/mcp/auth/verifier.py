"""DaimonJWTVerifier — FastMCP JWTVerifier subclass that adds account-existence
guard inside `verify_token` and stashes `tenant_id` in claims.

Why here rather than in FastMCP middleware: `AuthorizationError` raised from
`Middleware.on_request` is silently swallowed by FastMCP's tool-list / call
dispatch paths (`except AuthorizationError: continue`). The only wire-level
401/403 producer is `RequireAuthMiddleware`, which runs during verification.
So the only way to turn "unknown account" into a real HTTP 401 is to return
`None` from `verify_token` — which this subclass does.

Every failure mode (bad sig or expiry, malformed/missing sub, unknown
account, a revoked or refused token row) collapses to HTTP 401. That is how
an operator token stops working the moment its account is no longer a
server admin, its row is revoked, or it expires.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import AccountIdentityRow, McpTokenRow, Role
from daimon.core.stores.mcp_tokens import get_mcp_token
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Claim keys only this verifier writes, from the token's registry row. Any
# value a token carries under them is dropped before verification.
TOKEN_KIND_CLAIM = "daimon_token_kind"
TOKEN_JTI_CLAIM = "daimon_token_jti"
SCOPES_CLAIM = "daimon_scopes"


def token_row_refusal(
    row: McpTokenRow,
    *,
    claims: Mapping[str, object],
    identity: AccountIdentityRow,
    now: datetime,
) -> str | None:
    """Why a live registry row does not admit this token, or None when it does.

    An operator token acts for a server admin, so the account must still be
    one by stored role, and must have the platform user id every billing and
    pin check keys on (a token without one would read as the deployment's
    own unbilled operator path).
    """
    if row.expires_at is not None and row.expires_at <= now:
        return "expired"
    if row.kind == "agent":
        return None  # agent keys verify exactly as before this check existed
    if claims.get("kind") != row.kind:
        return "kind_mismatch"
    if row.account_id != identity.account_id or row.tenant_id != identity.tenant_id:
        return "account_mismatch"
    if row.kind == "cli":
        return None
    if identity.role is not Role.ADMIN:
        return "not_admin"
    if identity.platform_user_id is None:
        return "no_platform_user"
    if not row.scopes:
        return "no_scopes"
    return None


class DaimonJWTVerifier(JWTVerifier):
    """HS256 verifier + account-existence guard.

    On success, stashes `tenant_id` (from the account's DB row) into
    `AccessToken.claims` so downstream middleware can read it without a
    second DB query. A token with a jti must have a live registry row; its
    kind, jti and (for an operator token) scopes are stashed from the row.
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
        for key in (TOKEN_KIND_CLAIM, TOKEN_JTI_CLAIM, SCOPES_CLAIM):
            access.claims.pop(key, None)
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
            row: McpTokenRow | None = None
            jti = access.claims.get("jti")
            if isinstance(jti, str):
                try:
                    jti_uuid = uuid.UUID(jti)
                except ValueError:
                    return None
                row = await get_mcp_token(session, jti=jti_uuid)
                if row is None or row.revoked_at is not None:
                    return None
                refusal = token_row_refusal(
                    row, claims=access.claims, identity=identity_row, now=datetime.now(UTC)
                )
                if refusal is not None:
                    structlog.get_logger(__name__).info(
                        "mcp.token_refused", jti=str(row.jti), kind=row.kind, reason=refusal
                    )
                    return None
            elif access.claims.get("kind") is not None:
                return None  # only registered tokens carry a kind
            access.claims["tenant_id"] = str(identity_row.tenant_id)
            access.claims["role"] = identity_row.role.value
            access.claims["platform"] = identity_row.platform
            access.claims["external_id"] = identity_row.external_id
            if identity_row.platform_user_id is not None:
                access.claims["platform_user_id"] = identity_row.platform_user_id
            if row is not None:
                access.claims[TOKEN_KIND_CLAIM] = row.kind
                access.claims[TOKEN_JTI_CLAIM] = str(row.jti)
                if row.kind == "operator":
                    access.claims[SCOPES_CLAIM] = list(row.scopes)
        return access
