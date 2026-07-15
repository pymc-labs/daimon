"""FastMCP middleware that hydrates `AuthIdentity` into request state.

Runs only on requests that already passed `DaimonJWTVerifier.verify_token`
(account known). The middleware's job is to:
  1. Ask the injected `subject_resolver` for the caller's `sub` claim.
  2. Ask the injected `tenant_resolver` for the caller's `tenant_id` claim.
  3. Parse both as UUIDs (defensively — the verifier already guaranteed them).
  4. Ask `role_resolver` for the caller's `role` claim (stashed by verifier).
  5. Call `resolve_role` (pure sync) to map claim string to Role enum.
  6. Read platform/external_id/platform_user_id inline from injected claims (no DB call).
  7. Ask `agent_id_resolver` for the optional Phase 19 `agent_id` claim (D-10/D-27).
  8. Stash an `AuthIdentity` into `ctx.fastmcp_context.set_state("auth", ...)`.
  9. Call `enable_components` for admin sessions so admin-tagged tools are visible.

The resolvers are injected so tests can supply fixture-reading callables;
production plugs in `get_access_token().claims[...]`.
Tool handlers always read via `await ctx.get_state("auth")`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from daimon.adapters.mcp.auth.resolver import AuthIdentity, resolve_role
from daimon.core.stores.domain import Role
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms.visibility import disable_components, enable_components
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ClaimResolver = Callable[[MiddlewareContext], Awaitable[str | None]]
SubjectResolver = ClaimResolver


async def production_subject_resolver(context: MiddlewareContext) -> str | None:
    del context
    token = get_access_token()
    if token is None:
        return None
    sub = token.claims.get("sub")
    return sub if isinstance(sub, str) else None


async def production_tenant_resolver(context: MiddlewareContext) -> str | None:
    del context
    token = get_access_token()
    if token is None:
        return None
    tid = token.claims.get("tenant_id")
    return tid if isinstance(tid, str) else None


async def production_role_resolver(context: MiddlewareContext) -> str | None:
    del context
    token = get_access_token()
    if token is None:
        return None
    role = token.claims.get("role")
    return role if isinstance(role, str) else None


async def production_agent_id_resolver(context: MiddlewareContext) -> str | None:
    del context
    token = get_access_token()
    if token is None:
        return None
    agent_id = token.claims.get("agent_id")
    return agent_id if isinstance(agent_id, str) else None


async def production_is_admin_resolver(context: MiddlewareContext) -> str | None:
    del context
    token = get_access_token()
    if token is None:
        return None
    return "true" if token.claims.get("is_admin") is True else None


async def production_internal_resolver(context: MiddlewareContext) -> str | None:
    """Return "true" iff the token carries internal=True (the trusted-token discriminator).

    ``internal=True`` is emitted ONLY by ``mint_internal_mcp_token`` (CLI/scheduler/headless).
    A Discord vault token minted by ``mint_jwt`` never carries this claim. The admin gate
    keys on ``(role == ADMIN) OR (is_admin_claim AND internal_claim)`` so a stale pre-sweep
    Discord vault credential with ``is_admin=True`` and ``role=user`` is denied admin
    elevation, closing the #162 escalation independent of the 88-06 sweep.
    """
    del context
    token = get_access_token()
    if token is None:
        return None
    return "true" if token.claims.get("internal") is True else None


class IdentityMiddleware(Middleware):
    def __init__(
        self,
        *,
        subject_resolver: ClaimResolver,
        tenant_resolver: ClaimResolver,
        role_resolver: ClaimResolver,
        agent_id_resolver: ClaimResolver,
        is_admin_resolver: ClaimResolver,
        internal_resolver: ClaimResolver,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._subject_resolver = subject_resolver
        self._tenant_resolver = tenant_resolver
        self._role_resolver = role_resolver
        self._agent_id_resolver = agent_id_resolver
        self._is_admin_resolver = is_admin_resolver
        self._internal_resolver = internal_resolver
        self._sessionmaker = sessionmaker

    async def on_request(
        self,
        context: MiddlewareContext,
        call_next: CallNext,
    ) -> object:
        sub = await self._subject_resolver(context)
        if sub is None:
            raise AuthorizationError("missing sub after verifier")
        try:
            account_id = uuid.UUID(sub)
        except ValueError as e:
            raise AuthorizationError("malformed sub after verifier") from e

        tid = await self._tenant_resolver(context)
        if tid is None:
            raise AuthorizationError("missing tenant_id after verifier")
        try:
            tenant_id = uuid.UUID(tid)
        except ValueError as e:
            raise AuthorizationError("malformed tenant_id after verifier") from e

        fastmcp_ctx = context.fastmcp_context
        if fastmcp_ctx is None:
            raise AuthorizationError("missing fastmcp context on request")

        role_str = await self._role_resolver(context)
        role = resolve_role(role_str)
        # Read platform, external_id, platform_user_id from injected claims (no DB call)
        _token = get_access_token()
        platform = _token.claims.get("platform") if _token else None
        platform = platform if isinstance(platform, str) else None
        external_id = _token.claims.get("external_id") if _token else None
        external_id = external_id if isinstance(external_id, str) else None
        pu_claim = _token.claims.get("platform_user_id") if _token else None
        platform_user_id: str | None = pu_claim if isinstance(pu_claim, str) else None
        raw_agent_id = await self._agent_id_resolver(context)
        agent_id: uuid.UUID | None
        if raw_agent_id is None:
            agent_id = None
        else:
            try:
                agent_id = uuid.UUID(raw_agent_id)
            except (ValueError, TypeError):
                # Malformed claim is treated as absent (T-19-04-07).
                # Fail-closed downstream at the gcloud provider via NoBindingError.
                agent_id = None
        is_admin_claim = (await self._is_admin_resolver(context)) == "true"
        internal_claim = (await self._internal_resolver(context)) == "true"
        # Admin gate (#162 / ADMIN-02): a Discord vault token's baked is_admin claim alone
        # MUST NOT elevate a non-admin caller. The internal discriminator distinguishes
        # trusted internal tokens (CLI/scheduler/headless, minted by mint_internal_mcp_token)
        # from Discord vault tokens (minted by mint_jwt, which never emits internal=True).
        # Gate: DB role == ADMIN  OR  (is_admin claim AND internal claim).
        is_admin = (role == Role.ADMIN) or (is_admin_claim and internal_claim)
        identity = AuthIdentity(
            account_id=account_id,
            tenant_id=tenant_id,
            role=role,
            platform=platform,
            external_id=external_id,
            agent_id=agent_id,
            platform_user_id=platform_user_id,
            is_admin=is_admin,
        )
        await fastmcp_ctx.set_state("auth", identity, serializable=False)
        if is_admin:
            await enable_components(fastmcp_ctx, tags={"admin"})
        # Phase 77 PHASE-77-TOOLS-01: when agent_id is present (a verified
        # derived per-agent UUID), narrow the session to agent-chat-tagged
        # tools only. disable_components(match_all=True) then
        # enable_components(tags={"agent-chat"}) — later marks override earlier
        # so only the four agent-chat tools remain visible.
        # Fail-closed: malformed/absent agent_id is silently nulled at
        # l.141-144 above (T-19-04-07), so this branch is skipped entirely —
        # the session does NOT gain admin or agent-chat visibility.
        if agent_id is not None:
            await disable_components(fastmcp_ctx, match_all=True)
            await enable_components(fastmcp_ctx, tags={"agent-chat"})
        return await call_next(context)
