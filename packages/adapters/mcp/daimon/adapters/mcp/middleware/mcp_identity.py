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
from daimon.adapters.mcp.auth.verifier import (
    CHAT_EXECUTION_CLAIM,
    FOREIGN_HOME_CLAIM,
    SCOPES_CLAIM,
    TOKEN_JTI_CLAIM,
    TOKEN_KIND_CLAIM,
    TOKEN_LABEL_CLAIM,
)
from daimon.adapters.mcp.middleware.held_tools import held_tool_refusal, narrow_to_held_tools
from daimon.adapters.mcp.middleware.tool_rule_backstop import DENIAL_REASON, tool_rule_refusal
from daimon.core.admin_authority import resolve_authority
from daimon.core.anthropic_guard import AnthropicDisabled, without_anthropic
from daimon.core.channel_backend import is_selfhosted_channel
from daimon.core.config import Settings
from daimon.core.mcp_hold import HOLD_CLAIM, parse_hold_claim
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.operator_tokens import scope_tag
from daimon.core.security_audit import capture_decision, record_denial
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import McpTokenKind, Role
from daimon.core.stores.security_audit import SecurityAuditEntry, append_event
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn_hold import TurnHold
from fastmcp.exceptions import AuthorizationError, NotFoundError, ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.transforms.visibility import disable_components, enable_components
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

#: Audit reason for a call a held credential made to a tool off the allowlist.
HELD_DENIAL_REASON = "held_tool_not_allowed"

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

    Historical jti-less internal tokens are refused before this middleware.
    A Discord vault token minted by ``mint_jwt`` never carries this claim. The admin gate
    keys on ``(role == ADMIN) OR (is_admin_claim AND internal_claim)`` so a stale pre-sweep
    Discord vault credential with ``is_admin=True`` and ``role=user`` is denied admin
    elevation, closing the admin escalation independent of the credential sweep.
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
        tool_safety: ToolSafetyPolicy | None = None,
        public_url: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        # Agent-wide `deny` rules on Daimon's own tools are enforced here; see
        # `tool_rule_backstop`. Both unset (the default) leaves that off.
        self._settings = settings
        self._tool_safety = tool_safety
        self._public_url = public_url
        self._sessionmaker = sessionmaker
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

        def signed_uuid(name: str) -> uuid.UUID | None:
            # A missing or malformed claim is absent: its gates fail closed.
            value = _token.claims.get(name) if _token else None
            try:
                return uuid.UUID(value) if isinstance(value, str) else None
            except ValueError:
                return None

        # Dropping a malformed execution marker would make it look like a
        # personal credential. Reject its shape before constructing identity.
        if _token is not None:
            for binding in ("chat_agent_id", "routine_id", "execution_id", "slack_turn_context_id"):
                if _token.claims.get(binding) is not None and signed_uuid(binding) is None:
                    raise AuthorizationError("malformed execution binding")
            raw_session = _token.claims.get("chat_session_id")
            if raw_session is not None and (not isinstance(raw_session, str) or not raw_session):
                raise AuthorizationError("malformed chat session binding")

        # Keep chat execution identity separate from external agent credentials:
        # agent_id controls tool visibility and several authorization gates.
        chat_agent_id = signed_uuid("chat_agent_id")
        chat_execution = _token is not None and _token.claims.get(CHAT_EXECUTION_CLAIM) is True
        if chat_execution and (chat_agent_id is None or raw_agent_id is not None):
            raise AuthorizationError("malformed chat execution binding")
        agent_id: uuid.UUID | None
        if raw_agent_id is None:
            if (
                _token is not None
                and _token.claims.get(TOKEN_KIND_CLAIM) == "agent"
                and not chat_execution
            ):
                raise AuthorizationError("agent credential missing agent_id")
            agent_id = None
        else:
            try:
                agent_id = uuid.UUID(raw_agent_id)
            except (ValueError, TypeError):
                raise AuthorizationError("malformed agent_id after verifier") from None
        is_admin_claim = (await self._is_admin_resolver(context)) == "true"
        internal_claim = (await self._internal_resolver(context)) == "true"
        # Local operator roles remain valid. Platform accounts are rebound and
        # attested below; their stored role and signed internal claims never elevate.
        is_admin = (role == Role.ADMIN) or (is_admin_claim and internal_claim)
        is_platform_admin = False
        verified_role_ids: tuple[str, ...] = ()
        # Platform callers never inherit authority from a baked internal claim.
        # Bind to a current principal before the live attestation, also for agent keys.
        async with self._sessionmaker() as session:
            current = await get_account_with_tenant(session, account_id=account_id)
        if current is not None and current.platform in {"discord", "slack", "teams"}:
            platform, external_id = current.platform, current.external_id
            platform_user_id = current.platform_user_id
            is_admin = False
            if (
                self._settings is not None
                and current.tenant_id == tenant_id
                and platform_user_id is not None
            ):
                authority = await resolve_authority(
                    self._sessionmaker,
                    self._settings,
                    tenant_id=tenant_id,
                    platform_user_id=platform_user_id,
                )
                is_admin, is_platform_admin = authority.daimon_admin, authority.platform_admin
                verified_role_ids = authority.role_ids
            role = Role.ADMIN if is_admin else Role.USER
        elif platform in {"discord", "slack", "teams"}:
            is_admin = False
            role = Role.USER
        hold = _hold_from_claims(_token.claims if _token else {})
        raw_foreign_home = _token.claims.get(FOREIGN_HOME_CLAIM) if _token else None
        foreign_home_id = raw_foreign_home if isinstance(raw_foreign_home, str) else None
        if hold is not None or foreign_home_id is not None:
            # The stored role is never relied on for a held credential or for a
            # principal verified to belong to another organisation.
            is_admin = False
            is_platform_admin = False
            role = Role.USER
        slack_turn_context_id = (
            None if internal_claim or agent_id is not None else signed_uuid("slack_turn_context_id")
        )
        raw_bound_channel = _token.claims.get("bound_channel_id") if _token else None

        identity = AuthIdentity(
            routine_id=signed_uuid("routine_id"),
            execution_id=signed_uuid("execution_id"),
            account_id=account_id,
            tenant_id=tenant_id,
            role=role,
            platform=platform,
            external_id=external_id,
            agent_id=agent_id,
            chat_agent_id=chat_agent_id,
            anthropic_disabled=(
                _token is not None and _token.claims.get("anthropic_disabled") is True
            ),
            no_confirmation_surface=(
                _token is not None and _token.claims.get("no_confirmation_surface") is True
            ),
            slack_turn_context_id=slack_turn_context_id,
            chat_session_id=(
                _token.claims.get("chat_session_id")
                if _token is not None
                and isinstance(_token.claims.get("chat_session_id"), str)
                and agent_id is None
                and not internal_claim
                else None
            ),
            platform_user_id=platform_user_id,
            is_admin=is_admin,
            hold=hold,
            foreign_home_id=foreign_home_id,
            is_platform_admin=is_platform_admin,
            verified_role_ids=verified_role_ids,
            # Set by the verifier from the token's row; an agent key's only.
            bound_channel_id=(
                raw_bound_channel
                if isinstance(raw_bound_channel, str) and agent_id is not None
                else None
            ),
            **_token_fields(_token.claims if _token else {}),
        )
        await fastmcp_ctx.set_state("auth", identity, serializable=False)
        if context.method == "tools/call":
            # A proxied `call_tool` re-enters this middleware with the inner
            # tool's name, so this one check covers direct and proxied calls.
            rule_refusal = await tool_rule_refusal(
                self._sessionmaker,
                identity=identity,
                tool_name=str(getattr(context.message, "name", "")),
                policy=self._tool_safety,
                public_url=self._public_url,
            )
            if rule_refusal is not None:
                record_denial(DENIAL_REASON)
                raise ToolError(rule_refusal)
        # Each enable appends a rule to session state, which must be this
        # request's alone: StripSessionIdMiddleware (server.py) sees to it.
        if identity.hold is not None or identity.foreign_home_id is not None:
            # A principal marked foreign is narrowed even on an unheld credential:
            # it has no channel to be held to, so channel tools refuse it.
            return await self._resolve_held(context, call_next, identity)
        if identity.is_operator:
            return await self._resolve_operator(context, call_next, identity)
        if is_admin:
            await enable_components(fastmcp_ctx, tags={"admin"})
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
        return await self._dispatch_for_backend(context, call_next, identity)

    async def _dispatch_for_backend(
        self, context: MiddlewareContext, call_next: CallNext, identity: AuthIdentity
    ) -> object:
        if context.method not in {"tools/call", "resources/read", "prompts/get"}:
            return await call_next(context)
        if identity.no_confirmation_surface and context.method == "tools/call":
            from daimon.core.tool_safety import confirmation_gated_tools, self_carded_tools

            # `call_tool` re-enters here with the inner tool's name, so the proxy is covered.
            # A self-carded tool posts its own card instead of acting (`switch_cards`).
            if str(getattr(context.message, "name", "")) in (
                confirmation_gated_tools() - self_carded_tools()
            ):
                raise ToolError(
                    "This tool needs a confirmation card, which this conversation can't show."
                )
        selfhosted = identity.anthropic_disabled
        if identity.bound_channel_id is not None and identity.platform is not None:
            async with self._sessionmaker() as session:
                selfhosted = selfhosted or await is_selfhosted_channel(
                    session,
                    tenant_id=identity.tenant_id,
                    platform=identity.platform,
                    channel_id=identity.bound_channel_id,
                )
        if not selfhosted:
            return await call_next(context)
        try:
            with without_anthropic():
                return await call_next(context)
        except AnthropicDisabled as exc:
            raise ToolError(str(exc)) from exc

    async def _resolve_held(
        self, context: MiddlewareContext, call_next: CallNext, identity: AuthIdentity
    ) -> object:
        """Show a held identity only the allowlisted tools, and refuse any other call.

        Every tool is hidden first, so a tool added later is closed by default;
        the refusal covers a client that calls a hidden tool by name, including
        through the `call_tool` proxy, which re-enters this middleware with the
        inner tool's name.
        """
        fastmcp_ctx = context.fastmcp_context
        assert fastmcp_ctx is not None  # checked by _resolve_request
        if context.method == "tools/call":
            refusal = held_tool_refusal(str(getattr(context.message, "name", "")))
            if refusal is not None:
                record_denial(HELD_DENIAL_REASON)
                raise ToolError(refusal)
        await narrow_to_held_tools(fastmcp_ctx)
        return await self._dispatch_for_backend(context, call_next, identity)

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


def _hold_from_claims(claims: Mapping[str, object]) -> TurnHold | None:
    """The hold the verifier admitted, or None. An unreadable claim is refused."""
    raw = claims.get(HOLD_CLAIM)
    if raw is None:
        return None
    try:
        return parse_hold_claim(raw)
    except ValueError as exc:
        raise AuthorizationError("malformed hold claim after verifier") from exc


class _TokenFields(TypedDict):
    is_chat_execution: bool
    token_kind: McpTokenKind | None
    token_label: str | None
    token_jti: uuid.UUID | None
    scopes: frozenset[str]


_TOKEN_KINDS: dict[str, McpTokenKind] = {"agent": "agent", "operator": "operator", "cli": "cli"}


def _token_fields(claims: Mapping[str, object]) -> _TokenFields:
    """The registry-row fields the verifier stashed; absent for jti-less tokens."""
    kind = claims.get(TOKEN_KIND_CLAIM)
    jti = claims.get(TOKEN_JTI_CLAIM)
    scopes = claims.get(SCOPES_CLAIM)
    label = claims.get(TOKEN_LABEL_CLAIM)
    return _TokenFields(
        is_chat_execution=claims.get(CHAT_EXECUTION_CLAIM) is True,
        token_kind=_TOKEN_KINDS.get(kind) if isinstance(kind, str) else None,
        token_label=label if isinstance(label, str) else None,
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
