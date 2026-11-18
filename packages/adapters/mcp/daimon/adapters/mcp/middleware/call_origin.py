"""Tells every tool call where its turn started, for the inside-only settings rule.

A change to a private channel, or to its own agent, is allowed only from inside
that channel (`daimon.core.permissions.sealed_outside`). A chat tool proves where
it was called from by its turn origin (`get_verified_origin`). This sets that
origin once per call, so `mcp_subject` carries it into every gate.
"""

from __future__ import annotations

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import CALL_ORIGIN, origin_proves_presence
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin, list_running_origins
from daimon.core.authz import origin_ids
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

import mcp.types as mt

log = structlog.get_logger(__name__)


async def call_origin_ids(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: object
) -> frozenset[str]:
    """The channel and thread the call's turn runs in; empty when it proves none or is unattended.

    With several running turns of one session and no named origin, only what they all
    share counts. An agent key proves nothing: it keeps its old origin.
    """
    if auth.is_external_agent:
        return frozenset()
    named = origin_context_id if isinstance(origin_context_id, str) else None
    origin = await get_verified_origin(runtime, auth, named)
    origins = [origin] if origin is not None else await list_running_origins(runtime, auth)
    shared: frozenset[str] | None = None
    for found in origins:
        if not origin_proves_presence(auth, found):
            return frozenset()
        ids = origin_ids(found.parent_channel_id, found.thread_id)
        shared = ids if shared is None else shared & ids
    return shared or frozenset()


class CallOriginMiddleware(Middleware):
    """Sets `CALL_ORIGIN` for the duration of each tool call."""

    def __init__(self, runtime: McpRuntime) -> None:
        self._runtime = runtime

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        ctx = context.fastmcp_context
        auth = await ctx.get_state("auth") if ctx is not None else None
        if not isinstance(auth, AuthIdentity) or auth.chat_session_id is None:
            return await call_next(context)
        try:
            ids = await call_origin_ids(
                self._runtime, auth, (context.message.arguments or {}).get("origin_context_id")
            )
        except Exception:  # named boundary: no proven origin means a settings change is refused
            log.warning("call_origin.read_failed", exc_info=True)
            ids = frozenset[str]()
        token = CALL_ORIGIN.set(ids)
        try:
            return await call_next(context)
        finally:
            CALL_ORIGIN.reset(token)
