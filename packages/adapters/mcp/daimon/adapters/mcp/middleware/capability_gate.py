"""The server's own check that a capability feature Off refuses its tools, on every path.

Claude sessions are not offered these tools (`SessionToolset`), but Gemini and OpenAI turns,
external MCP clients and agent keys reach the server directly, so the server refuses the call.
`call_tool` re-enters the middleware with the inner tool's name, so proxied calls are covered.

tools/list is filtered for every caller except a chat-execution credential: the native turn
checks this catalogue against its consent, less the tools Off at its place, which a template's
allowlist leaves out (`turn/template_mcp.py`); Claude sessions hide them in their session
config. Such a caller is refused at call time.

A tool whose setup this server lacks (`SERVER_SETUP`) leaves every list, that credential's too,
and its call is refused as not set up: a worker can't judge that setup, and the server's
settings, so that catalogue, stay fixed while it runs. So does a tool whose feature does not
exist in the caller's workspace (`in_workspace`). Where pages are absent or off, the watch
tools are listed without their page source and words (`without_pages`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin, list_running_origins
from daimon.adapters.mcp.tools._pageless import without_pages
from daimon.core.anthropic_guard import anthropic_disabled
from daimon.core.features.deployment import in_workspace
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeaturePlace, resolve_features, server_setup_met
from daimon.core.features.tool_gate import (
    GATED_KEYS,
    gated_tools,
    hides_tools,
    server_gate_key,
)
from daimon.core.ma_resolver import find_agent_by_derived_uuid_cached
from daimon.core.mux_backend import resource_scope
from daimon.core.rule_views import routine_feature_place
from daimon.core.security_audit import record_denial
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.routines import get_routine
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.core.stores.tenants import get_tenant
from daimon.core.tool_safety import RENAMED_DAIMON_TOOLS
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool, ToolResult

import mcp.types as mt

log = structlog.get_logger(__name__)

#: Audit reason for a call refused because its capability is Off.
DENIAL_REASON: Final = "capability_off"
#: Audit reason for a call refused because this deployment lacks its capability's setup.
UNSET_REASON: Final = "capability_unset"


def _refusal(tool: str, key: str) -> str:
    return (
        f"{FEATURES[key].name} is turned off here, so {tool} can't run. Nothing was done. "
        "Ask an admin or an owner to turn it on."
    )


def _unset_refusal(tool: str, key: str) -> str:
    return (
        f"{FEATURES[key].name} isn't set up here yet, so {tool} can't run. Nothing was done. "
        "Ask whoever runs Daimon to set it up."
    )


def _absent_refusal(tool: str) -> str:
    return f"{tool} isn't available here, so nothing was done."


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
    agent key's agent and channel, or else the workspace.

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
            tenant = await get_tenant(session, auth.tenant_id)
        if routine is not None and tenant is not None:
            # As its session's toolset reads it (`headless_runner.py`).
            return [routine_feature_place(routine, platform=tenant.platform)]
    agent_id = None if auth.agent_id is None else await _key_agent_id(runtime, auth, auth.agent_id)
    return [
        FeaturePlace(auth.tenant_id, auth.platform or "", token_channel_id(auth), agent_id=agent_id)
    ]


async def _key_agent_id(runtime: McpRuntime, auth: AuthIdentity, derived: uuid.UUID) -> str | None:
    """An agent key's own agent id, so its agent-tier values apply rather than the channel's.
    Cached; a failed lookup is logged and leaves the agent tier out, never refusing."""
    key = (auth.tenant_id, derived)
    if (cached := runtime.resolver_cache.derived_agent_ids.get(key)) is not None:
        return cached
    try:
        if auth.anthropic_disabled or anthropic_disabled():
            record = await SelfHostedAgents(runtime.session_factory).find_by_derived_uuid(
                resource_scope(tenant_id=str(auth.tenant_id)), derived
            )
            found = None if record is None else record.ref.id
        else:
            agent = await find_agent_by_derived_uuid_cached(
                runtime.client,
                runtime.resolver_cache,
                tenant_id=auth.tenant_id,
                agent_id=derived,
                records=runtime.session_factory,
            )
            found = None if agent is None else agent.id
    except Exception:  # named boundary: the agent tier is extra; its lookup never refuses
        log.warning("capability_gate.key_agent_lookup_failed", exc_info=True)
        return None
    if found is not None:
        runtime.resolver_cache.derived_agent_ids[key] = found
    return found


def _lists_everything(auth: AuthIdentity) -> bool:
    """A chat-execution credential: its native turn checks the whole catalogue (`mint`)."""
    return auth.chat_agent_id is not None and auth.agent_id is None


class CapabilityGateMiddleware(Middleware):
    """Refuses a call to a tool whose capability is Off at the caller's place or not set up."""

    def __init__(self, runtime: McpRuntime) -> None:
        self._runtime = runtime

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool = context.message.name
        renamed = RENAMED_DAIMON_TOOLS.get(tool)
        if renamed is not None:
            # A conversation that began before the rename can still ask for the old
            # name. Say where the tool went instead of "unknown tool".
            raise ToolError(f"{tool} is now called {renamed}. Call {renamed} instead.")
        key = server_gate_key(tool)
        ctx = context.fastmcp_context
        auth = await ctx.get_state("auth") if key is not None and ctx is not None else None
        if key is None or not isinstance(auth, AuthIdentity):
            return await call_next(context)
        if not in_workspace(FEATURES[key], self._runtime.settings, auth.tenant_id):
            record_denial(UNSET_REASON)
            raise ToolError(_absent_refusal(tool))
        if not server_setup_met(FEATURES[key], self._runtime.settings):
            record_denial(UNSET_REASON)
            raise ToolError(_unset_refusal(tool, key))
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
        if not isinstance(auth, AuthIdentity):
            return tools
        settings = self._runtime.settings
        unset = gated_tools(
            k
            for k in GATED_KEYS
            if not server_setup_met(FEATURES[k], settings)
            or not in_workspace(FEATURES[k], settings, auth.tenant_id)
        )
        tools = [t for t in tools if t.name not in unset or server_gate_key(t.name) is None]
        pageless = not in_workspace(FEATURES["artifacts"], settings, auth.tenant_id)
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
            found = []
        # Where pages are absent or off, the watch tools keep their names but never name pages.
        if pageless or any(hides_tools(place.get("artifacts")) for place in found):
            tools = [without_pages(t) for t in tools]
        if _lists_everything(auth) or not found:
            return tools
        off = gated_tools(
            key for key in GATED_KEYS if any(hides_tools(place.get(key)) for place in found)
        )
        return [t for t in tools if t.name not in off or server_gate_key(t.name) is None]
