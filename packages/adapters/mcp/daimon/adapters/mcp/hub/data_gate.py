"""Offer the hub's data tools only to a caller with `data_reads` On in some workspace.

The server's capability gate hides Off tools from `tools/list`; a hub caller reaches several
workspaces, so a data tool stays listed while any of them has it On. A call is judged in the
workspace it names (`hub_workspace_tool`).
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from daimon.adapters.mcp.hub.identity import HUB_AUTH_STATE_KEY, HubIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.data_tools import DATA_READS, DATA_TOOLS
from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeaturePlace, resolve_features, server_setup_met
from daimon.core.features.tool_gate import hides_tools
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import Tool

import mcp.types as mt

log = structlog.get_logger(__name__)


class HubDataGate(Middleware):
    def __init__(self, runtime: McpRuntime) -> None:
        self._runtime = runtime

    async def _any_on(self, hub: HubIdentity) -> bool:
        settings = self._runtime.settings
        if not server_setup_met(FEATURES[DATA_READS], settings):
            return False
        for tenant in hub.tenants:
            place = FeaturePlace(tenant.tenant_id, hub.platform, None)
            found = await resolve_features(
                self._runtime.session_factory, settings, place, keys=(DATA_READS,)
            )
            if not hides_tools(found.get(DATA_READS)):
                return True
        return False

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        ctx = context.fastmcp_context
        hub = None if ctx is None else await ctx.get_state(HUB_AUTH_STATE_KEY)
        try:
            if isinstance(hub, HubIdentity) and not await self._any_on(hub):
                return [t for t in tools if t.name not in DATA_TOOLS]
        except Exception:  # named boundary: an unreadable feature hides the tools, never the hub
            log.warning("hub.data_gate_read_failed", exc_info=True)
            return [t for t in tools if t.name not in DATA_TOOLS]
        return tools
