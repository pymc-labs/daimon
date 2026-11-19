"""`web_fetch` for agents whose backend has no page fetch of its own.

Hidden unless the credential is a provider turn's (`HOST_WEB_FETCH_TAG`). In a
conversation the place's `tool.web_fetch` feature decides. An unattended run
follows its built-in tools: only a routine run that matched an admin's opt-in, where
`unattended_capabilities` is on, keeps them (the signed `host_builtins` flag).
A held turn never lists it (`mcp_hold`).
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.features.snapshot import FeaturePlace, resolve_features
from daimon.core.host_web_fetch import (
    FETCH_TIMEOUT_S,
    HOST_WEB_FETCH_TAG,
    WebFetchError,
    fetch_page,
)
from daimon.core.mcp_tool_call import pinned_http_client
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
        if not await _routine_keeps_builtins(auth):
            raise ToolError("Opening web pages is off for this routine. Ask an admin.")
        return
    if origin_context_id is None:
        raise ToolError("Opening web pages needs a conversation.")
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    if not origin.attended and not auth.host_builtins:
        raise ToolError("Opening web pages is off when nobody is in the conversation.")
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


async def _routine_keeps_builtins(auth: AuthIdentity) -> bool:
    """Whether the run's own decision kept built-ins on.

    The runner signs `host_builtins` into the credential only after the run
    matched the operator's pin and `unattended_capabilities` allowed it, so a
    pin row alone, or a run that did not match, never opens them.
    """
    return auth.routine_id is not None and auth.host_builtins


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
