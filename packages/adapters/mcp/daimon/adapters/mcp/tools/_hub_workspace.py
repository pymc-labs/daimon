"""Tools the hub shares with the server, taking a `workspace` as well.

A hub caller reaches several workspaces, so a shared tool must say which one it runs in.
`hub_workspace_tool` registers the same function on the hub with that argument added: it
resolves the workspace among the caller's own (anything else reads as not found), builds the
caller's identity there as `list_daimons` does, applies the server's capability gate for the
tool and sets the identity where the function reads it (`_auth`).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, get_type_hints

from daimon.adapters.mcp.hub.identity import (
    HubIdentity,
    _hub_auth,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.middleware.capability_gate import require_capability
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.hub import _auth_for  # pyright: ignore[reportPrivateUsage]
from daimon.core.features.tool_gate import server_gate_key
from daimon.core.hub_identity import HubTenant
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

_NOT_FOUND = "workspace not found"
_DOC = (
    "\n\n`workspace`: the workspace to use, by name or id as `list_daimons` shows them; "
    "leave it out when you reach only one."
)


def pick_workspace(hub: HubIdentity, wanted: str | None) -> HubTenant:
    """The caller's workspace named by `wanted` (name, id or tenant id), or their only one."""
    if wanted is None:
        if len(hub.tenants) == 1:
            return hub.tenants[0]
        names = ", ".join(sorted(t.workspace_name for t in hub.tenants))
        raise ToolError(f"Name the workspace: {names}." if names else _NOT_FOUND)
    key = wanted.strip().casefold()
    found = [
        t
        for t in hub.tenants
        if key in (t.workspace_id.casefold(), str(t.tenant_id), t.workspace_name.casefold())
    ]
    if len(found) != 1:
        raise ToolError(_NOT_FOUND)
    return found[0]


type _Tool = Callable[..., Coroutine[Any, Any, Any]]


def hub_workspace_tool(mcp: FastMCP, runtime: McpRuntime) -> Callable[[_Tool], _Tool]:
    """A decorator that registers a server tool on the hub, with a `workspace` argument."""

    def register(fn: _Tool) -> _Tool:
        hints = get_type_hints(fn, include_extras=True)
        params = [
            p.replace(annotation=hints[p.name]) for p in inspect.signature(fn).parameters.values()
        ]
        ctx_name = next(p.name for p in params if p.annotation is Context)
        pick = inspect.Parameter(
            "workspace",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None,
            annotation=Annotated[str | None, Field(description="The workspace to use.")],
        )
        at = next((i for i, p in enumerate(params) if p.default is not p.empty), len(params))
        params.insert(at, pick)
        gate = server_gate_key(fn.__name__)

        async def wrapper(**kwargs: Any) -> Any:  # noqa: ANN401  # a tool's own arguments and result
            ctx: Context = kwargs[ctx_name]
            hub = await _hub_auth(ctx)
            tenant = pick_workspace(hub, kwargs.pop("workspace"))
            auth = await _auth_for(runtime, hub, tenant, None)
            if gate is not None:
                await require_capability(runtime, fn.__name__, gate, auth)
            await ctx.set_state("auth", auth, serializable=False)
            return await fn(**kwargs)

        wrapper.__name__ = fn.__name__
        wrapper.__qualname__ = fn.__qualname__
        wrapper.__module__ = fn.__module__
        wrapper.__doc__ = (inspect.getdoc(fn) or "") + _DOC
        wrapper.__signature__ = inspect.Signature(  # pyright: ignore[reportFunctionMemberAccess]
            params, return_annotation=hints.get("return", inspect.Signature.empty)
        )
        wrapper.__annotations__ = {p.name: p.annotation for p in params} | (
            {"return": hints["return"]} if "return" in hints else {}
        )
        mcp.tool(wrapper)
        return fn

    return register
