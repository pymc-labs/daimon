"""Tenant summary: the balance, funding mode and every configured channel in one read.

For server admins and operator tokens holding ``tenant:read``, so an
integration can show a workspace's state without one call per channel. The
read lives in ``daimon.core.tenant_summary``. A chat turn's isolation keeps
other isolated channels' agent and environment names from it, as in
``list_agents`` and ``list_environments``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._isolation import load_caller_isolation
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.channel_environments import load_hidden_environment_names
from daimon.core.errors import StoreError
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from daimon.core.tenant_summary import TenantSummary, load_tenant_summary
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def _hide_across_isolation(
    runtime: McpRuntime, auth: AuthIdentity, summary: TenantSummary
) -> TenantSummary:
    """Blank the names the caller's isolation keeps from it; an operator sees all."""
    if auth.is_operator:
        return summary
    caller = await load_caller_isolation(runtime, auth)
    if not caller.is_active:
        return summary
    async with runtime.session_factory() as session:
        hidden = await load_hidden_environment_names(
            session, tenant_id=auth.tenant_id, viewer=caller, default=runtime.deployment_default
        )
    channels = [
        replace(
            row,
            agent_name=row.agent_name if caller.sees(row.agent_name) else None,
            environment_name=None if row.environment_name in hidden else row.environment_name,
        )
        for row in summary.channels
    ]
    default_agent = summary.default_agent if caller.sees(summary.default_agent) else None
    return replace(summary, default_agent=default_agent, channels=channels)


async def _get_tenant_summary_impl(runtime: McpRuntime, auth: AuthIdentity) -> TenantSummary:
    require_scope(auth, "tenant:read")
    _require_admin(auth)
    async with runtime.session_factory() as session:
        try:
            summary = await load_tenant_summary(
                session,
                tenant_id=auth.tenant_id,
                default=runtime.deployment_default,
                now=datetime.now(UTC),
            )
        except StoreError as exc:
            raise ToolError("internal: the caller's tenant no longer exists") from exc
        except AccessPolicyUnreadable as exc:
            raise ToolError("the workspace access policy could not be read") from exc
    return await _hide_across_isolation(runtime, auth, summary)


def register_tenant_summary_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("tenant:read")})
    async def get_tenant_summary(ctx: Context) -> TenantSummary:  # pyright: ignore[reportUnusedFunction]
        """Summarize this server or workspace: balance, funding mode and each channel. Admin-only.

        Lists every channel with its own agent or environment setting, a
        spending budget, channel admins or confidential status, with the agent and environment that
        apply there, the roles and members administering it, and the budget's
        limit, window and spend (``budget`` is null without one), and the
        live timed promo credit with when each grant ends. Money is a decimal
        string. Agent and environment names a confidential channel keeps from
        this conversation read as null.
        """
        return await _get_tenant_summary_impl(runtime, await _auth(ctx))
