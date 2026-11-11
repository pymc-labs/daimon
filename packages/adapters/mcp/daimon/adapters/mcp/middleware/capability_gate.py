"""The server's own check that a capability feature Off refuses its tools, on every path.

Claude sessions are not offered these tools (`SessionToolset`), but Gemini and OpenAI turns,
external MCP clients and agent keys reach the server directly, so the server refuses the call.
`call_tool` re-enters the middleware with the inner tool's name, so proxied calls are covered.

tools/list is filtered for every caller except a chat-execution credential: the native turn
checks its live catalogue against its allowlist (`turn/runtime_manifest.py`), and Claude
sessions already hide the tools in their session config. Such a caller is refused at call time.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin, list_running_origins
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.features.tool_gate import (
    GATED_KEYS,
    gated_tools,
    hides_tools,
    server_gate_key,
)
from daimon.core.security_audit import record_denial
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.routines import get_routine
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult

import mcp.types as mt

log = structlog.get_logger(__name__)

#: Audit reason for a call refused because its capability is Off.
DENIAL_REASON: Final = "capability_off"


def _refusal(tool: str, key: str) -> str:
    return (
        f"{FEATURES[key].name} is turned off here, so {tool} can't run. Nothing was done. "
        "Ask an admin or an owner to turn it on."
    )


_UNREADABLE: Final = (
    "Couldn't check whether this tool is turned on here. Nothing was done. Try again."
)


def _origin_place(auth: AuthIdentity, origin: TurnOriginRow) -> FeaturePlace:
    return FeaturePlace.of(
        auth.tenant_id,
        origin.platform,
        origin.parent_channel_id,
        origin.thread_id,
        agent_id=origin.responder_ma_agent_id,
    )


async def caller_places(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: object
) -> list[FeaturePlace]:
    """Where the call may run, from verified facts only: its turn origin, its routine, an
    agent key's channel, or else the workspace.

    Several running turns of one session, when none is named, cannot be told apart. Turns
    in one channel and thread are one place; otherwise each place is returned and a gate
    treats the feature as Off if any of them has it Off."""
    named = origin_context_id if isinstance(origin_context_id, str) else None
    origin = await get_verified_origin(runtime, auth, named)
    if origin is None:
        running = await list_running_origins(runtime, auth)
        if running:
            return list(dict.fromkeys(_origin_place(auth, found) for found in running))
    else:
        return [_origin_place(auth, origin)]
    if auth.routine_id is not None:
        async with runtime.session_factory() as session:
            routine = await get_routine(session, auth.routine_id, tenant_id=auth.tenant_id)
        if routine is not None:
            # As the scheduler reads it (`scheduler.py`).
            return [FeaturePlace(auth.tenant_id, "", routine.channel_id, agent_id=routine.agent_id)]
    return [FeaturePlace(auth.tenant_id, auth.platform or "", token_channel_id(auth))]


def _lists_everything(auth: AuthIdentity) -> bool:
    """A chat-execution credential: its native catalogue must match its allowlist."""
    return auth.chat_agent_id is not None and auth.agent_id is None


class CapabilityGateMiddleware(Middleware):
    """Refuses a call to a tool whose capability is Off at the caller's place."""

    def __init__(self, runtime: McpRuntime) -> None:
        self._runtime = runtime

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool = context.message.name
        key = server_gate_key(tool)
        ctx = context.fastmcp_context
        auth = await ctx.get_state("auth") if key is not None and ctx is not None else None
        if key is None or not isinstance(auth, AuthIdentity):
            return await call_next(context)
        try:
            places = await caller_places(
                self._runtime, auth, (context.message.arguments or {}).get("origin_context_id")
            )
            found = [
                (
                    await resolve_features(
                        self._runtime.session_factory, self._runtime.settings, place, keys=(key,)
                    )
                ).get(key)
                for place in places
            ]
        except Exception as exc:  # named boundary: an unreadable feature refuses the call
            log.warning("capability_gate.read_failed", tool=tool, exc_info=True)
            record_denial(DENIAL_REASON)
            raise ToolError(_UNREADABLE) from exc
        if any(hides_tools(feature) for feature in found):
            record_denial(DENIAL_REASON)
            raise ToolError(_refusal(tool, key))
        return await call_next(context)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        ctx = context.fastmcp_context
        auth = await ctx.get_state("auth") if ctx is not None else None
        if not isinstance(auth, AuthIdentity) or _lists_everything(auth):
            return tools
        try:
            places = await caller_places(self._runtime, auth, None)
            found = [
                await resolve_features(
                    self._runtime.session_factory, self._runtime.settings, place, keys=GATED_KEYS
                )
                for place in places
            ]
        except Exception:  # named boundary: the list stays whole; every call is still checked
            log.warning("capability_gate.list_read_failed", exc_info=True)
            return tools
        off = gated_tools(
            key for key in GATED_KEYS if any(hides_tools(place.get(key)) for place in found)
        )
        return [t for t in tools if t.name not in off or server_gate_key(t.name) is None]
