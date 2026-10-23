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

import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog
from daimon.core.config import McpSettings
from daimon.core.mcp_auth import (
    CHAT_TOKEN_TTL,
    chat_token_label,
    registered_agent_claims_match,
)
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import AccountIdentityRow, McpTokenRow, Role
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.core.stores.security_audit import append_event
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Claim keys only this verifier writes, from the token's registry row. Any
# value a token carries under them is dropped before verification.
CHAT_EXECUTION_CLAIM = "daimon_chat_execution"
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
        if row.account_id != identity.account_id or row.tenant_id != identity.tenant_id:
            return "account_mismatch"
        chat = claims.get("chat_agent_id")
        if row.label == chat_token_label(row.jti):
            expiry, issued = claims.get("exp"), claims.get("iat")
            if (
                chat != row.agent_id
                or claims.get("agent_id") is not None
                or claims.get("internal") is not None
                or type(expiry) is not int
                or type(issued) is not int
                or row.expires_at is None
                or expiry != int(row.expires_at.timestamp())
                or not 0 < expiry - issued <= int(CHAT_TOKEN_TTL.total_seconds())
            ):
                return "chat_binding_mismatch"
        elif chat is not None:
            if row.scopes or not registered_agent_claims_match(
                row, claims, now=now, chat_execution=True, allow_session_grant=True
            ):
                return "chat_binding_mismatch"
        elif claims.get("agent_id") != row.agent_id:
            return "agent_mismatch"
        if row.channel_id is not None and identity.role is not Role.ADMIN:
            return "admin_required"
        return None
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


def _token_diagnostics(claims: Mapping[str, object]) -> dict[str, object]:
    """Bounded, value-free telemetry; callers pass only verified claims."""
    issued = claims.get("iat")
    age_bucket = "unknown"
    if type(issued) is int:
        age = int(datetime.now(UTC).timestamp()) - issued
        if age < 0:
            age_bucket = "future"
        else:
            age_bucket = ">90d"
            for days, label in [(1, "<1d"), (7, "1-7d"), (30, "7-30d"), (90, "30-90d")]:
                if age < days * 86400:
                    age_bucket = label
                    break
    return {
        "age_bucket": age_bucket,
        "has_sub": "sub" in claims,
        "sub_is_str": isinstance(claims.get("sub"), str),
        "has_jti": "jti" in claims,
        "jti_is_str": isinstance(claims.get("jti"), str),
        "has_iat": "iat" in claims,
        "iat_is_int": type(issued) is int,
        "has_exp": "exp" in claims,
        "exp_is_int": type(claims.get("exp")) is int,
        "has_internal": "internal" in claims,
        "internal_true": claims.get("internal") is True,
        "has_chat_agent_id": "chat_agent_id" in claims,
        "has_agent_id": "agent_id" in claims,
        "has_kind": "kind" in claims,
    }


def _reject_token(reason: str, claims: Mapping[str, object]) -> None:
    structlog.get_logger(__name__).warning(
        "mcp.token_rejected", reason=reason, **_token_diagnostics(claims)
    )
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
        legacy_token_cutoff: datetime | None = None,
        allow_legacy_tokens: bool = False,
    ) -> None:
        super().__init__(
            public_key=secret.decode(),
            algorithm="HS256",
        )
        # The dependency logs client_id/sub on expiry. Keep verification there,
        # but route every refusal through our value-free diagnostics instead.
        self.logger = logging.Logger(f"{__name__}.signature")
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False
        policy = McpSettings(
            legacy_token_cutoff=legacy_token_cutoff,
            allow_legacy_tokens=allow_legacy_tokens,
        )
        self._legacy_token_cutoff = int(
            (policy.legacy_token_cutoff or datetime.now(UTC)).timestamp()
        )
        self._allow_legacy_tokens = policy.allow_legacy_tokens
        self._sessionmaker = sessionmaker

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None:
            return _reject_token("signature_or_expiry", {})
        # Only the registry row sets these, never a claim.
        for key in (
            CHAT_EXECUTION_CLAIM,
            TOKEN_KIND_CLAIM,
            TOKEN_JTI_CLAIM,
            SCOPES_CLAIM,
            "bound_channel_id",
            "platform_role_ids",
        ):
            access.claims.pop(key, None)
        sub = access.claims.get("sub")
        if not isinstance(sub, str):
            return _reject_token("missing_subject", access.claims)
        try:
            account_id = uuid.UUID(sub)
        except ValueError:
            return _reject_token("malformed_subject", access.claims)
        async with self._sessionmaker() as session:
            identity_row = await get_account_with_tenant(session, account_id=account_id)
            if identity_row is None:
                return _reject_token("unknown_account", access.claims)
            row: McpTokenRow | None = None
            jti = access.claims.get("jti")
            legacy_window = (
                self._allow_legacy_tokens
                and int(datetime.now(UTC).timestamp()) < self._legacy_token_cutoff
            )
            if isinstance(jti, str):
                if (
                    access.claims.get("chat_agent_id") is not None
                    and type(access.claims.get("exp")) is not int
                ):
                    return _reject_token("chat_expiry_shape", access.claims)
                try:
                    jti_uuid = uuid.UUID(jti)
                except ValueError:
                    return _reject_token("malformed_jti", access.claims)
                row = await get_mcp_token(session, jti=jti_uuid)
                if row is None:
                    return _reject_token("unknown_jti", access.claims)
            elif "jti" in access.claims or access.claims.get("kind") is not None:
                return _reject_token(
                    "malformed_registry_claims", access.claims
                )  # malformed registry ids never fall back to legacy auth
            elif "internal" in access.claims:
                return _reject_token("unregistered_internal", access.claims)
            else:
                issued = access.claims.get("iat")
                if (
                    not legacy_window
                    or type(issued) is not int
                    or issued >= self._legacy_token_cutoff
                ):
                    return _reject_token("unregistered_token", access.claims)
                structlog.get_logger(__name__).warning(
                    "mcp.legacy_token_admitted",
                    reason="bounded_non_internal_rollout",
                    **_token_diagnostics(access.claims),
                )
        if row is not None:
            refusal = token_row_refusal(
                row, claims=access.claims, identity=identity_row, now=datetime.now(UTC)
            )
            if refusal is not None:
                await self._audit_refusal(row, identity=identity_row, reason=refusal)
                return _reject_token(refusal, access.claims)
        access.claims["tenant_id"] = str(identity_row.tenant_id)
        access.claims["role"] = identity_row.role.value
        access.claims["platform"] = identity_row.platform
        access.claims["external_id"] = identity_row.external_id
        if identity_row.platform_user_id is not None:
            access.claims["platform_user_id"] = identity_row.platform_user_id
        if row is not None:
            # This distinction is established only after validating the registry
            # binding. A signed claim alone cannot turn an external key into chat.
            access.claims[CHAT_EXECUTION_CLAIM] = (
                row.kind == "agent" and access.claims.get("chat_agent_id") is not None
            )
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
                error_type=type(exc).__name__,
            )
