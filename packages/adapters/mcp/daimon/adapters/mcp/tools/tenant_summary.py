"""Tenant summary: the balance, funding mode and every configured channel in one read.

For server admins and operator tokens holding ``tenant:read``, so an
integration can show a workspace's state without one call per channel. The
read lives in ``daimon.core.tenant_summary``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.errors import StoreError
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from daimon.core.tenant_summary import TenantSummary, load_tenant_summary
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def _get_tenant_summary_impl(runtime: McpRuntime, auth: AuthIdentity) -> TenantSummary:
    require_scope(auth, "tenant:read")
    _require_admin(auth)
    async with runtime.session_factory() as session:
        try:
            return await load_tenant_summary(
                session,
                tenant_id=auth.tenant_id,
                default=runtime.deployment_default,
                now=datetime.now(UTC),
            )
        except StoreError as exc:
            raise ToolError("internal: the caller's tenant no longer exists") from exc
        except AccessPolicyUnreadable as exc:
            raise ToolError("the workspace access policy could not be read") from exc


def register_tenant_summary_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("tenant:read")})
    async def get_tenant_summary(ctx: Context) -> TenantSummary:  # pyright: ignore[reportUnusedFunction]
        """Summarize this server or workspace: balance, funding mode and each channel. Admin-only.

        Lists every channel with its own agent or environment setting, a
        spending budget, channel admins or isolation, with the agent and environment that
        apply there, the roles and members administering it, and the budget's
        limit, window and spend (``budget`` is null without one). Money is a
        decimal string.
        """
        return await _get_tenant_summary_impl(runtime, await _auth(ctx))
