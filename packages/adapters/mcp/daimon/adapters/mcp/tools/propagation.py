"""Propagation tools: set and clear agent defaults at workspace or channel scope.

``register_propagation_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.

These tools close the SC5 conversational-parity gap (Phase 83, D-07): there was no
MCP tool for propagation / set-default. The same core scoped-config writes that the
modal scope picker uses (``set_fields`` / ``unset_fields`` / ``get_scope``) are now
reachable conversationally via ``@bot help me set up``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import ChannelScopeRef, TenantScopeRef
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
from fastmcp import Context, FastMCP


@dataclass(frozen=True)
class SetDefaultResult:
    """Result returned from set_agent_default."""

    scope: str
    """'workspace' or 'channel:<channel_id>'"""
    agent_name: str
    """The newly-set default agent name."""
    previous_agent_name: str | None
    """The agent name that was overwritten, or None if the scope had no prior default."""


@dataclass(frozen=True)
class ClearDefaultResult:
    """Result returned from clear_agent_default."""

    scope: str
    """'workspace' or 'channel:<channel_id>'"""
    cleared: bool
    """True if there was an agent_name to clear; False if the scope had none."""


async def _set_agent_default_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_name: str,
    channel_id: str | None,
) -> SetDefaultResult:
    _require_admin(auth)

    tenant_id: uuid.UUID = auth.tenant_id
    if channel_id is not None:
        scope: ChannelScopeRef | TenantScopeRef = ChannelScopeRef(
            tenant_id=tenant_id, channel_id=channel_id
        )
        scope_label = f"channel:{channel_id}"
    else:
        scope = TenantScopeRef(tenant_id=tenant_id)
        scope_label = "workspace"

    async with runtime.session_factory.begin() as session:
        prior = await get_scope(session, scope=scope)
        prior_agent: str | None = prior.agent_name if prior is not None else None
        await set_fields(
            session,
            scope=scope,
            tenant_id=tenant_id,
            agent_name=agent_name,
            mode="agent",
            actor_account_id=auth.account_id,
        )

    return SetDefaultResult(
        scope=scope_label,
        agent_name=agent_name,
        previous_agent_name=prior_agent,
    )


async def _clear_agent_default_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel_id: str | None,
) -> ClearDefaultResult:
    _require_admin(auth)

    tenant_id: uuid.UUID = auth.tenant_id
    if channel_id is not None:
        scope: ChannelScopeRef | TenantScopeRef = ChannelScopeRef(
            tenant_id=tenant_id, channel_id=channel_id
        )
        scope_label = f"channel:{channel_id}"
    else:
        scope = TenantScopeRef(tenant_id=tenant_id)
        scope_label = "workspace"

    async with runtime.session_factory.begin() as session:
        prior = await get_scope(session, scope=scope)
        had_default = prior is not None and prior.agent_name is not None
        if had_default:
            await unset_fields(
                session,
                scope=scope,
                fields=["agent_name"],
                actor_account_id=auth.account_id,
            )

    return ClearDefaultResult(scope=scope_label, cleared=had_default)


def register_propagation_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def set_agent_default(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        channel_id: str | None = None,
    ) -> SetDefaultResult:
        """Set the agent that responds by default in a channel or the whole workspace.

        When ``channel_id`` is provided the default is scoped to that channel;
        omit it to set the workspace-wide default.  Any existing default at the
        chosen scope is replaced (last-write-wins; an audit stamp is recorded by
        core).  Requires Manage Server (admin).
        """
        return await _set_agent_default_impl(runtime, await _auth(ctx), agent_name, channel_id)

    @mcp.tool(tags={"admin"})
    async def clear_agent_default(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str | None = None,
    ) -> ClearDefaultResult:
        """Remove the agent default from a channel or the whole workspace.

        When ``channel_id`` is provided only that channel's default is cleared;
        omit it to clear the workspace-wide default.  If the scope had no
        default the call is a no-op (idempotent).  Requires Manage Server (admin).
        """
        return await _clear_agent_default_impl(runtime, await _auth(ctx), channel_id)
