"""FastMCP middleware that hydrates `AuthIdentity` into request state.

Runs only on requests that already passed `DaimonJWTVerifier.verify_token`
(account known). The middleware's job is to:
  1. Ask the injected `subject_resolver` for the caller's `sub` claim.
  2. Ask the injected `tenant_resolver` for the caller's `tenant_id` claim.
  3. Parse both as UUIDs (defensively — the verifier already guaranteed them).
  4. Ask `role_resolver` for the caller's `role` claim (stashed by verifier).
  5. Call `resolve_role` (pure sync) to map claim string to Role enum.
  6. Read platform/external_id/platform_user_id inline from injected claims (no DB call).
  7. Ask `agent_id_resolver` for the optional `agent_id` claim.
  8. Stash an `AuthIdentity` into `ctx.fastmcp_context.set_state("auth", ...)`.
  9. Call `enable_components` for admin sessions so admin-tagged tools are visible,
     and for channel admins so the `channel-admin` subset of them is.
     An operator token instead sees only the tools tagged with its scopes,
     and its tool calls are rate limited per jti.

The resolvers are injected so tests can supply fixture-reading callables;
production plugs in `get_access_token().claims[...]`.
Tool handlers always read via `await ctx.get_state("auth")`.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import TypedDict, cast

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity, resolve_role
from daimon.adapters.mcp.auth.verifier import SCOPES_CLAIM, TOKEN_JTI_CLAIM, TOKEN_KIND_CLAIM
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.operator_tokens import scope_tag
from daimon.core.security_audit import capture_decision, record_denial
from daimon.core.stores.domain import McpTokenKind, Role
from daimon.core.stores.security_audit import SecurityAuditEntry, append_event
from fastmcp.exceptions import AuthorizationError, NotFoundError, ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms.visibility import disable_components, enable_components
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

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
        operator_rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._operator_rate_limiter = operator_rate_limiter
        self._subject_resolver = subject_resolver
        self._tenant_resolver = tenant_resolver
        self._role_resolver = role_resolver
        self._agent_id_resolver = agent_id_resolver
        self._is_admin_resolver = is_admin_resolver
        self._internal_resolver = internal_resolver
        bind = sessionmaker.kw.get("bind")
        # A caller may supply a factory bound to one transaction/connection.
        # Background writes must acquire their own connection, never concurrently
        # use the tool's connection or join its rollback scope.
        self._audit_sessionmaker = (
            async_sessionmaker(bind.engine, expire_on_commit=False)
            if isinstance(bind, AsyncConnection)
            else sessionmaker
        )
        self._audit_tasks: set[asyncio.Task[None]] = set()
        self._audit_timeout = 2.0
        self._audit_max_pending = 128
        self._audit_slots = asyncio.Semaphore(2)

    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> object:
        if context.method not in {"tools/call", "tools/list"}:
            return await self._resolve_request(context, call_next)
        # The verifier has already authenticated the tenant claim. Requests with
        # no attributable tenant cannot be placed in another tenant's audit log.
        tenant_id = _uuid_or_none(await self._tenant_resolver(context))
        if tenant_id is None:
            return await self._resolve_request(context, call_next)
        account_id = _uuid_or_none(await self._subject_resolver(context))
        agent_id = _uuid_or_none(await self._agent_id_resolver(context))
        token = get_access_token()
        claims = token.claims if token is not None else {}
        chat_agent = claims.get("chat_agent_id")
        if agent_id is None and isinstance(chat_agent, str):
            agent_id = _uuid_or_none(chat_agent)
        platform = claims.get("platform")
        user_id = claims.get("platform_user_id")
        token_kind = claims.get(TOKEN_KIND_CLAIM)
        token_jti = claims.get(TOKEN_JTI_CLAIM)
        name = (
            getattr(context.message, "name", None)
            if context.method == "tools/call"
            else "tools/list"
        )
        tool_name = (
            name
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", name)
            else "<invalid>"
        )
        with capture_decision() as decision:
            try:
                result = await self._resolve_request(context, call_next)
                if getattr(result, "isError", False) is True and decision.reason in {
                    "completed",
                    "policy_allow",
                }:
                    decision.reason = "tool_error"
                return result
            except BaseException as exc:
                if isinstance(exc, (AuthorizationError, NotFoundError)):
                    decision.denied = True
                if decision.reason in {"completed", "policy_allow"}:
                    decision.reason = (
                        "authorization_error"
                        if isinstance(exc, (AuthorizationError, NotFoundError))
                        else "tool_error"
                        if isinstance(exc, ToolError)
                        else "cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "request_error"
                    )
                raise
            finally:
                # Snapshot only metadata before leaving the request-local scope.
                # Scheduling does no I/O and never waits for the database.
                self._queue_audit(
                    SecurityAuditEntry(
                        tenant_id=tenant_id,
                        account_id=account_id,
                        agent_id=agent_id,
                        platform=platform if isinstance(platform, str) else None,
                        platform_user_id=user_id if isinstance(user_id, str) else None,
                        tool_name=tool_name,
                        operation=decision.operation,
                        outcome=(
                            "denied"
                            if decision.denied
                            else "error"
                            if decision.reason in {"tool_error", "request_error", "cancelled"}
                            else "allowed"
                        ),
                        reason=decision.reason,
                        token_kind=token_kind if isinstance(token_kind, str) else None,
                        token_jti=(
                            _uuid_or_none(token_jti) if isinstance(token_jti, str) else None
                        ),
                        scope=decision.scope,
                    )
                )

    def _queue_audit(self, event: SecurityAuditEntry) -> None:
        if len(self._audit_tasks) >= self._audit_max_pending:
            structlog.get_logger(__name__).warning(
                "security_audit.write_failed",
                tenant_id=str(event.tenant_id),
                error_type="QueueFull",
            )
            return
        task = asyncio.create_task(self._write_audit(event), name="security-audit-write")
        self._audit_tasks.add(task)
        task.add_done_callback(self._audit_tasks.discard)

    async def _write_audit(self, event: SecurityAuditEntry) -> None:
        try:
            async with (
                asyncio.timeout(self._audit_timeout),
                self._audit_slots,
                self._audit_sessionmaker() as session,
                session.begin(),
            ):
                await append_event(session, **event.model_dump())
        except (Exception, asyncio.CancelledError) as exc:
            structlog.get_logger(__name__).warning(
                "security_audit.write_failed",
                tenant_id=str(event.tenant_id),
                error_type=type(exc).__name__,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def drain_audit(self) -> None:
        """Wait for tracked, timeout-bounded writes during orderly teardown."""
        if self._audit_tasks:
            await asyncio.gather(*tuple(self._audit_tasks), return_exceptions=True)

    async def _resolve_request(
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
        # Keep chat execution identity separate from external agent credentials:
        # agent_id controls tool visibility and several authorization gates.
        raw_chat_agent_id = _token.claims.get("chat_agent_id") if _token else None
        chat_agent_id: uuid.UUID | None = None
        if isinstance(raw_chat_agent_id, str):
            try:
                chat_agent_id = uuid.UUID(raw_chat_agent_id)
            except ValueError:
                chat_agent_id = None  # Fail closed at the Google broker.
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
        slack_turn_context_id: uuid.UUID | None = None
        raw_slack_context = _token.claims.get("slack_turn_context_id") if _token else None
        if isinstance(raw_slack_context, str) and not internal_claim and agent_id is None:
            try:
                slack_turn_context_id = uuid.UUID(raw_slack_context)
            except ValueError:
                slack_turn_context_id = None  # Malformed claims fail closed.
        raw_role_ids = _token.claims.get("platform_role_ids") if _token else None
        platform_role_ids = (
            tuple(str(value) for value in cast(list[object], raw_role_ids))
            if isinstance(raw_role_ids, list)
            else ()
        )
        # Only a chat identity administers channels; an agent credential never does.
        raw_administered = _token.claims.get("administered_channel_ids") if _token else None
        administered_channel_ids = (
            frozenset(str(value) for value in cast(list[object], raw_administered))
            if isinstance(raw_administered, list) and not is_admin and agent_id is None
            else frozenset[str]()
        )
        identity = AuthIdentity(
            account_id=account_id,
            tenant_id=tenant_id,
            role=role,
            platform=platform,
            external_id=external_id,
            agent_id=agent_id,
            chat_agent_id=chat_agent_id,
            slack_turn_context_id=slack_turn_context_id,
            platform_user_id=platform_user_id,
            is_admin=is_admin,
            platform_role_ids=platform_role_ids,
            administered_channel_ids=administered_channel_ids,
            **_token_fields(_token.claims if _token else {}),
        )
        await fastmcp_ctx.set_state("auth", identity, serializable=False)
        # Each enable appends a rule to session state, which must be this
        # request's alone: StripSessionIdMiddleware (server.py) sees to it.
        if identity.is_operator:
            return await self._resolve_operator(context, call_next, identity)
        if is_admin:
            await enable_components(fastmcp_ctx, tags={"admin"})
        elif identity.is_channel_admin:
            # The admin tools a channel admin may call for their own channels.
            await enable_components(fastmcp_ctx, tags={"channel-admin"})
        # Enable the caller's platform tag (deny-by-default baselines live in
        # server.py). A CLI token's platform="cli" matches no baseline tag,
        # so this enable is a no-op for CLI — the intended outcome: CLI loses
        # the discord/slack-tagged tools without any special-casing here.
        if platform is not None:
            await enable_components(fastmcp_ctx, tags={platform})
        # Only dedicated external agent_id credentials select the restricted
        # agent-chat surface. Chat execution identity leaves visibility intact.
        # Malformed identity fails closed at tools that require an agent.
        if agent_id is not None:
            await disable_components(fastmcp_ctx, match_all=True)
            await enable_components(fastmcp_ctx, tags={"agent-chat"})
        return await call_next(context)

    async def _resolve_operator(
        self, context: MiddlewareContext, call_next: CallNext, identity: AuthIdentity
    ) -> object:
        """Show an operator token only its scopes' tools, and rate limit its calls.

        Every tool is disabled first, admin and platform tools included, so a
        scope removed from the row since the last request is gone too. The
        tools re-check the scope; this filter is what the token can discover.
        """
        fastmcp_ctx = context.fastmcp_context
        assert fastmcp_ctx is not None  # checked by _resolve_request
        if (
            context.method == "tools/call"
            and self._operator_rate_limiter is not None
            and not self._operator_rate_limiter.check_and_record(str(identity.token_jti))
        ):
            record_denial("rate_limited")
            raise ToolError("This operator token is making too many calls. Retry in a minute.")
        await disable_components(fastmcp_ctx, match_all=True)
        if identity.scopes:
            await enable_components(fastmcp_ctx, tags={scope_tag(s) for s in identity.scopes})
        return await call_next(context)


class _TokenFields(TypedDict):
    token_kind: McpTokenKind | None
    token_jti: uuid.UUID | None
    scopes: frozenset[str]


_TOKEN_KINDS: dict[str, McpTokenKind] = {"agent": "agent", "operator": "operator", "cli": "cli"}


def _token_fields(claims: Mapping[str, object]) -> _TokenFields:
    """The registry-row fields the verifier stashed; absent for jti-less tokens."""
    kind = claims.get(TOKEN_KIND_CLAIM)
    jti = claims.get(TOKEN_JTI_CLAIM)
    scopes = claims.get(SCOPES_CLAIM)
    return _TokenFields(
        token_kind=_TOKEN_KINDS.get(kind) if isinstance(kind, str) else None,
        token_jti=_uuid_or_none(jti) if isinstance(jti, str) else None,
        scopes=(
            frozenset(s for s in cast("list[object]", scopes) if isinstance(s, str))
            if isinstance(scopes, list)
            else frozenset()
        ),
    )


def _uuid_or_none(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw is not None else None
    except ValueError:
        return None
