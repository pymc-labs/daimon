"""DaimonJWTVerifier — FastMCP JWTVerifier subclass that adds account-existence
guard inside `verify_token` and stashes `tenant_id` in claims.

Why here rather than in FastMCP middleware: `AuthorizationError` raised from
`Middleware.on_request` is silently swallowed by FastMCP's tool-list / call
dispatch paths (`except AuthorizationError: continue`). The only wire-level
401/403 producer is `RequireAuthMiddleware`, which runs during verification.
So the only way to turn "unknown account" into a real HTTP 401 is to return
`None` from `verify_token` — which this subclass does.

Every failure mode (bad sig or expiry, malformed/missing sub, unknown
account, a revoked or refused token row) collapses to HTTP 401, and a refused
token row is also written to `security_audit_events`. An operator token's
admin check reads the account's stored role, which the account's next
platform turn refreshes, so a demoted admin's token keeps working until then
or until it expires; `daimon mcp revoke-token` stops it on its next request.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog
from daimon.core.channel_admins import CHANNEL_ADMIN_PLATFORMS
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.channel_admins import list_administered_channel_ids
from daimon.core.stores.domain import AccountIdentityRow, McpTokenRow, Role
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.stores.security_audit import append_event
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Claim keys only this verifier writes, from the token's registry row. Any
# value a token carries under them is dropped before verification.
TOKEN_KIND_CLAIM = "daimon_token_kind"
TOKEN_JTI_CLAIM = "daimon_token_jti"
SCOPES_CLAIM = "daimon_scopes"

REFUSAL_AUDIT_TOOL = "auth/verify"
"""The audit row's tool name for a refusal: the verifier never sees the tool."""


def token_row_refusal(
    row: McpTokenRow,
    *,
    claims: Mapping[str, object],
    identity: AccountIdentityRow,
    now: datetime,
) -> str | None:
    """Why a registry row does not admit this token, or None when it does.

    An operator token acts for a server admin, so the account must still be
    one by stored role, and must have the platform user id every billing and
    pin check keys on (a token without one would read as the deployment's
    own unbilled operator path).
    """
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None and row.expires_at <= now:
        return "expired"
    if row.platform is not None and row.platform != identity.platform:
        return "platform_mismatch"  # a channel binding this deployment never minted
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
    kind, jti, scopes (for an operator token) and bound channel (for an agent
    key minted in one) are stashed from the row.
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
        # Only the registry row sets these, never a claim.
        for key in (TOKEN_KIND_CLAIM, TOKEN_JTI_CLAIM, SCOPES_CLAIM, "bound_channel_id"):
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
                if row is None:
                    return None
            elif access.claims.get("kind") is not None:
                return None  # only registered tokens carry a kind
            administered_channel_ids = (
                sorted(
                    await list_administered_channel_ids(
                        session,
                        tenant_id=identity_row.tenant_id,
                        platform=identity_row.platform,
                        platform_user_id=identity_row.platform_user_id,
                        role_ids=identity_row.platform_role_ids,
                    )
                )
                if identity_row.role is not Role.ADMIN
                and identity_row.platform in CHANNEL_ADMIN_PLATFORMS
                and identity_row.platform_user_id is not None
                else []
            )
        if row is not None:
            refusal = token_row_refusal(
                row, claims=access.claims, identity=identity_row, now=datetime.now(UTC)
            )
            if refusal is not None:
                await self._audit_refusal(row, identity=identity_row, reason=refusal)
                return None
        access.claims["tenant_id"] = str(identity_row.tenant_id)
        access.claims["role"] = identity_row.role.value
        access.claims["platform"] = identity_row.platform
        access.claims["external_id"] = identity_row.external_id
        if identity_row.platform_user_id is not None:
            access.claims["platform_user_id"] = identity_row.platform_user_id
        # Always overwritten, so a token can never carry its own grant.
        access.claims["platform_role_ids"] = list(identity_row.platform_role_ids)
        access.claims["administered_channel_ids"] = administered_channel_ids
        if row is not None:
            access.claims[TOKEN_KIND_CLAIM] = row.kind
            access.claims[TOKEN_JTI_CLAIM] = str(row.jti)
            if row.kind == "operator":
                access.claims[SCOPES_CLAIM] = list(row.scopes)
            if row.channel_id is not None:
                access.claims["bound_channel_id"] = row.channel_id
        return access

    async def _audit_refusal(
        self, row: McpTokenRow, *, identity: AccountIdentityRow, reason: str
    ) -> None:
        """Record a refused registry row under its own tenant, never the token itself."""
        log = structlog.get_logger(__name__)
        log.info("mcp.token_refused", jti=str(row.jti), kind=row.kind, reason=reason)
        same_account = identity.account_id == row.account_id
        try:
            async with self._sessionmaker.begin() as session:
                await append_event(
                    session,
                    tenant_id=row.tenant_id,
                    account_id=row.account_id,
                    agent_id=uuid.UUID(row.agent_id) if row.agent_id is not None else None,
                    platform=identity.platform if same_account else None,
                    platform_user_id=identity.platform_user_id if same_account else None,
                    tool_name=REFUSAL_AUDIT_TOOL,
                    operation=None,
                    outcome="denied",
                    reason=reason,
                    token_kind=row.kind,
                    token_jti=row.jti,
                )
        except Exception as exc:  # boundary: a lost audit row must not turn the 401 into a 500
            log.warning(
                "security_audit.write_failed",
                tenant_id=str(row.tenant_id),
                error_type=type(exc).__name__,
            )
