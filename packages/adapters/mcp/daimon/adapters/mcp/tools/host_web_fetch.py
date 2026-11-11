"""`web_fetch` for agents whose backend has no page fetch of its own.

Hidden unless the credential is a provider turn's (`HOST_WEB_FETCH_TAG`). In a
conversation the place's `tool.web_fetch` feature decides. An unattended run
follows its built-in tools: only a routine an admin opted in, where
`unattended_capabilities` is on, keeps them. A held turn never lists it (`mcp_hold`).
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.features.snapshot import FeaturePlace, place_guardrail, resolve_features
from daimon.core.host_web_fetch import (
    FETCH_TIMEOUT_S,
    HOST_WEB_FETCH_TAG,
    WebFetchError,
    fetch_page,
)
from daimon.core.mcp_tool_call import pinned_http_client
from daimon.core.stores.routines import get_routine
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_FEATURE = "tool.web_fetch"


async def check_web_fetch_allowed(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> None:
    """Refuse unless a provider turn may open pages here."""
    if not auth.no_confirmation_surface:
        raise ToolError("Opening web pages is not available for this agent.")
    if auth.routine_id is not None or auth.execution_id is not None:
        if not await _routine_keeps_builtins(runtime, auth):
            raise ToolError("Opening web pages is off for this routine. Ask an admin.")
        return
    if origin_context_id is None:
        raise ToolError("Opening web pages needs a conversation.")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    features = await resolve_features(
        runtime.session_factory,
        runtime.settings,
        FeaturePlace.of(
            auth.tenant_id,
            origin.platform,
            origin.parent_channel_id,
            origin.thread_id,
            agent_id=origin.responder_ma_agent_id,
        ),
        keys=(_FEATURE,),
    )
    if not features.on(_FEATURE):
        raise ToolError("Opening web pages is off here.")


async def _routine_keeps_builtins(runtime: McpRuntime, auth: AuthIdentity) -> bool:
    if auth.routine_id is None:
        return False
    async with runtime.session_factory() as session:
        row = await get_routine(session, auth.routine_id, tenant_id=auth.tenant_id)
    if row is None or not row.sandbox_tools_enabled:
        return False
    place = FeaturePlace.of(auth.tenant_id, auth.platform or "routine", None, agent_id=row.agent_id)
    found = await place_guardrail(
        runtime.session_factory, runtime.settings, place, "unattended_capabilities"
    )
    return found.on


def register_host_web_fetch_tool(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={HOST_WEB_FETCH_TAG})
    async def web_fetch(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, url: str, origin_context_id: str | None = None
    ) -> str:
        """Open a public https web page and return its text.

        Long pages are cut short. Pages that are not text are refused.
        """
        await check_web_fetch_allowed(runtime, await _auth(ctx), origin_context_id)
        try:
            async with pinned_http_client(timeout=FETCH_TIMEOUT_S) as client:
                return await fetch_page(url, client=client)
        except WebFetchError as exc:
            raise ToolError(str(exc)) from exc
