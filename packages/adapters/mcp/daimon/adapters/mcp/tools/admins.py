"""Tenant-wide Daimon admin delegation, with fresh platform-only mutation gates."""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.admin_authority import resolve_authority
from daimon.core.stores.daimon_admins import AdminGrant, SubjectKind, change_grant
from daimon.core.stores.daimon_admins import list_admins as list_grants
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def change_admin(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    subject_id: str,
    subject_kind: SubjectKind = "user",
    revoke: bool = False,
) -> None:
    """A fresh attestation; claims, an agent key and delegated authority cannot grant."""
    if (
        not auth.is_personal_user
        or auth.is_operator
        or auth.platform_user_id is None
        or auth.platform not in {"discord", "slack", "teams"}
    ):
        raise ToolError(
            "Only a platform admin acting directly with a personal user credential "
            "may change Daimon admins."
        )
    authority = await resolve_authority(
        runtime.session_factory,
        runtime.settings,
        tenant_id=auth.tenant_id,
        platform_user_id=auth.platform_user_id,
    )
    if authority.platform_admin is not True:
        raise ToolError("Only platform admins may grant or revoke Daimon admins.")
    async with runtime.session_factory.begin() as session:
        await change_grant(
            session,
            tenant_id=auth.tenant_id,
            platform=auth.platform,
            subject_kind=subject_kind,
            subject_id=subject_id,
            actor_user_id=auth.platform_user_id,
            is_platform_admin=authority.platform_admin,
            revoke=revoke,
        )


def register_admin_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={*scope_tags("tenant:read")})
    async def list_admins(ctx: Context) -> list[AdminGrant]:  # pyright: ignore[reportUnusedFunction]
        """List this tenant's explicit Daimon admin grants (any member).

        Platform admins always have Daimon authority live and are not stored in
        this list. A Discord role grant applies only while the member holds it.
        """
        auth = await _auth(ctx)
        require_scope(auth, "tenant:read")
        async with runtime.session_factory() as session:
            return await list_grants(session, tenant_id=auth.tenant_id)

    @mcp.tool(tags={"admin"})
    async def grant_admin(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        subject_id: str,
        subject_kind: SubjectKind = "user",
    ) -> str:
        """Grant Daimon admin to a user or Discord role (platform admins only).

        Only live platform admins using a personal CLI credential may grant.
        Chat, routines and all agent-run credentials are refused. Delegates cannot delegate.
        This grants configuration access; billing and spend caps remain platform-only.
        """
        await change_admin(
            runtime, await _auth(ctx), subject_id=subject_id, subject_kind=subject_kind
        )
        return "Daimon admin granted."

    @mcp.tool(tags={"admin"})
    async def revoke_admin(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        subject_id: str,
        subject_kind: SubjectKind = "user",
    ) -> str:
        """Revoke an explicit Daimon admin grant immediately (platform admins only).

        Requires a personal CLI credential; chat and routines are refused.
        Deletes the grant and records its audit atomically; delegated authority is never cached.
        Platform authority cannot be revoked; remaining grants still apply.
        """
        await change_admin(
            runtime, await _auth(ctx), subject_id=subject_id, subject_kind=subject_kind, revoke=True
        )
        return "Explicit Daimon admin grant revoked. Platform admins retain access."
