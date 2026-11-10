"""The MCP side of press-applies confirmation (`daimon.core.host_calls`).

A provider-turn credential has no confirmation card, so a gated Daimon tool is not
refused here when the gate is on: the call is checked, recorded and carded, and the
model gets a waiting result. The wake turn's one-shot `confirmed_call` credential
later runs exactly that call, consumed once, on exactly that tool and arguments.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import structlog
from daimon.adapters.mcp.confirmed_call import host_confirmed_call
from daimon.core import host_calls
from daimon.core.features.snapshot import deployment_on
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.security_audit import record_denial
from daimon.core.stores import host_call_cards as cards
from daimon.core.stores.mcp_tokens import revoke_mcp_token
from daimon.core.stores.turn_origins import get_session_origin
from daimon.core.switch_cards import SWITCH_CARD_MAX_S
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, host_call_carded_tools
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, MiddlewareContext
from fastmcp.tools import ToolResult

from mcp.types import TextContent

if TYPE_CHECKING:
    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.runtime import McpRuntime

log = structlog.get_logger()

MISMATCH_TEXT = "This call isn't the one that was approved. Nothing ran."
_OUTCOME_MAX = 2000


def _call_of(context: MiddlewareContext) -> tuple[str, dict[str, Any]]:
    # `call_tool` re-enters the middleware with the inner tool's name and arguments.
    name = str(getattr(context.message, "name", ""))
    raw: object = getattr(context.message, "arguments", None)
    return name, dict(cast("Mapping[str, Any]", raw)) if isinstance(raw, Mapping) else {}


def _refuse(reason: str, text: str = host_calls.REFUSED_TEXT) -> ToolError:
    record_denial(f"host_call_{reason}")
    log.info("host_call.refused", reason=reason)
    return ToolError(text)


def schema_problem(schema: Mapping[str, Any], args: Mapping[str, Any]) -> str | None:
    """Why `args` can't be a call of this tool; the full type check runs when it executes."""
    properties: Mapping[str, Any] = schema.get("properties") or {}
    missing = [key for key in schema.get("required") or () if key not in args]
    if missing:
        return f"Missing arguments: {', '.join(sorted(missing))}. Nothing was asked."
    if schema.get("additionalProperties") is not True:
        unknown = [key for key in args if key not in properties]
        if unknown:
            return f"Unknown arguments: {', '.join(sorted(unknown))}. Nothing was asked."
    return None


def _result_text(result: object) -> str:
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return "Done."
    parts = [item.text for item in content if isinstance(item, TextContent)]  # pyright: ignore[reportUnknownVariableType]
    return ("\n".join(parts) or "Done.")[:_OUTCOME_MAX]  # pyright: ignore[reportUnknownArgumentType]


class HostCallGate:
    """Record-and-card a gated call, or run the one approved call; one per server."""

    def __init__(self, runtime: McpRuntime, *, public_url: str) -> None:
        self._runtime = runtime
        self._server = public_url

    def _digest(self, name: str, args: Mapping[str, Any], agent_id: uuid.UUID) -> str:
        return host_calls.canonical_call_digest(
            route=host_calls.ROUTE_DAIMON,
            server=self._server,
            tool=name,
            args=args,
            executing_agent_id=agent_id,
        )

    async def run_confirmed(
        self, context: MiddlewareContext, call_next: CallNext, identity: AuthIdentity
    ) -> object:
        """Consume the approved record this credential names, then run it once."""
        name, args = _call_of(context)
        card_id = identity.confirmed_call_id
        if card_id is None or identity.chat_agent_id is None or context.method != "tools/call":
            raise _refuse("confirmed_shape", MISMATCH_TEXT)
        now = datetime.now(UTC)
        async with self._runtime.session_factory.begin() as session:
            row = await cards.consume(
                session,
                card_id=card_id,
                tool_name=name,
                args_digest=self._digest(name, args, identity.chat_agent_id),
                executing_agent_id=identity.chat_agent_id,
                now=now,
            )
            if row is not None and identity.token_jti is not None:
                # One call per credential: nothing else may run under it.
                await revoke_mcp_token(session, jti=identity.token_jti, now=now)
        if row is None:
            raise _refuse("confirmed_mismatch", MISMATCH_TEXT)
        token = host_confirmed_call.set(row.id)
        try:
            result = await call_next(context)
        except ToolError as exc:
            await self._finish(row.id, "failed", str(exc)[:_OUTCOME_MAX])
            raise
        except Exception:
            # Anything else may have half-run: record it as failed, never retried.
            await self._finish(row.id, "failed", host_calls.CRASHED_TEXT)
            raise
        finally:
            host_confirmed_call.reset(token)
        await self._finish(row.id, "applied", _result_text(result))
        log.info("host_call.applied", card_id=str(row.id), tool=name)
        return result

    async def _finish(self, card_id: uuid.UUID, status: cards.Finished, outcome: str) -> None:
        async with self._runtime.session_factory.begin() as session:
            await cards.finish(session, card_id=card_id, status=status, outcome=outcome)

    async def ask(self, context: MiddlewareContext, identity: AuthIdentity) -> ToolResult:
        """Record the gated call and post its card; fail closed on anything missing."""
        from daimon.adapters.mcp.tools._own_card import post_own_card
        from daimon.core.turn.approvals import tool_call_verdict
        from daimon.core.turn.unattended_capabilities import protected_tools

        name, args = _call_of(context)
        settings = self._runtime.settings
        platform = identity.platform or ""
        if not deployment_on(settings, "host_call_gate", platform):
            raise _refuse("off")
        if name not in host_call_carded_tools():
            # A reach approval is tallied under the asking turn, which is gone by the press.
            raise _refuse("reach")
        if (
            identity.chat_agent_id is None
            or identity.chat_session_id is None
            or identity.platform_user_id is None
            or identity.hold is not None
        ):
            raise _refuse("no_claim")
        now = datetime.now(UTC)
        async with self._runtime.session_factory() as session:
            origin = await get_session_origin(
                session,
                tenant_id=identity.tenant_id,
                account_id=identity.requester_account_id or identity.account_id,
                platform=platform,
                chat_session_id=identity.chat_session_id,
                now=now,
            )
        if (
            origin is None
            or derive_agent_uuid(
                tenant_id=origin.tenant_id, ma_agent_id=origin.responder_ma_agent_id
            )
            != identity.chat_agent_id
        ):
            raise _refuse("no_origin")
        fastmcp_ctx = context.fastmcp_context
        tool = None if fastmcp_ctx is None else await fastmcp_ctx.fastmcp.get_tool(name)
        if tool is None:
            raise _refuse("no_tool")
        if (problem := schema_problem(tool.parameters, args)) is not None:
            raise ToolError(problem)
        policy = settings.tool_safety
        verdict = await tool_call_verdict(
            policy,
            ToolCall(
                tool_use_id=f"host-{uuid.uuid4()}",
                server_name=DAIMON_SERVER_NAME,
                tool_name=name,
                input=args,
            ),
            attended=origin.attended,
            trusted_servers=frozenset({DAIMON_SERVER_NAME}),
            confirmed_tools=protected_tools(policy),
            agent_rule=None,
        )
        if verdict.outcome != "ask":
            # A gated tool is never `allow` here; `deny` is an unattended or operator refusal.
            raise _refuse(f"verdict_{verdict.reason}")
        expires_at = now + timedelta(seconds=min(policy.confirmation_timeout_s, SWITCH_CARD_MAX_S))
        async with self._runtime.session_factory.begin() as session:
            created = await cards.create_card(
                session,
                tenant_id=identity.tenant_id,
                origin_id=origin.id,
                requester_account_id=origin.account_id,
                requester_platform_user_id=identity.platform_user_id,
                platform=origin.platform,
                channel_id=origin.parent_channel_id,
                thread_id=origin.thread_id,
                executing_agent_id=identity.chat_agent_id,
                executing_ma_agent_id=origin.responder_ma_agent_id,
                agent_name=origin.responder_name,
                tool_name=name,
                args_digest=self._digest(name, args, identity.chat_agent_id),
                reference={
                    "route": host_calls.ROUTE_DAIMON,
                    "server": self._server,
                    "args": args,
                    # An admin's ask is pressed by a person who must still be one.
                    "admin": identity.is_admin,
                },
                expires_at=expires_at,
            )
        person = "the person who asked"
        if created is None:
            raise _refuse("too_many", host_calls.TOO_MANY_TEXT.format(person=person))
        if created.fresh:
            posted = await host_calls.post_call_card(
                self._runtime.session_factory,
                created.row,
                post=lambda card: post_own_card(self._runtime, identity, origin=origin, card=card),
            )
            if not posted:
                raise _refuse("post_failed", host_calls.NOT_RUN_TEXT)
        log.info("host_call.recorded", card_id=str(created.row.id), tool=name)
        return ToolResult(content=[TextContent(type="text", text=host_calls.waiting_text(person))])
