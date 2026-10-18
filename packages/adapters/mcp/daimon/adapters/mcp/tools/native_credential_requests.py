"""Explicit registration on a dedicated native MCP endpoint, disabled by default.

No existing server calls this function. An operator supplies the authenticated
factory and native delivery/registry; the legacy endpoint is never overridden.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.turn.native_credential_requests import (
    NativeCredentialAdapter,
    NativeCredentialOrigin,
    NativeOfferReceipt,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.profile import Profile
from mux.errors import ScopeViolation, UnsupportedCapability


@dataclass(frozen=True)
class NativeCredentialGrant:
    adapter: NativeCredentialAdapter
    origin: NativeCredentialOrigin


type GrantFactory = Callable[[AuthIdentity, str], Awaitable[NativeCredentialGrant]]


async def register_native_credential_tools(
    mcp: FastMCP,
    *,
    profile: Profile,
    auth_resolver: Callable[[Context], Awaitable[AuthIdentity]],
    operator_enabled: bool = False,
    grants: GrantFactory | None = None,
) -> tuple[str, ...]:
    """Install offer tools only on an explicitly supplied separate native server.

    The factory derives scope/binding/catalog identity from verified native
    execution, never tool target strings. Missing factories fail before install;
    colliding legacy names fail rather than changing their dispatch/ordering.
    """
    if operator_enabled is not True:
        return ()
    if grants is None:
        raise UnsupportedCapability(
            (f"{profile.provider}_native_credential_grant",), profile.profile_id
        )
    names = ("github_connect", "request_agent_key")
    if set(names).intersection(tool.name for tool in await mcp.list_tools()):
        raise ValueError(
            "native credential tools require a separate explicitly selected MCP server"
        )

    async def offer(
        ctx: Context,
        origin_context_id: str,
        kind: Literal["github", "env"],
        target: str,
    ) -> NativeOfferReceipt:
        auth = await auth_resolver(ctx)
        if auth.agent_id is not None or auth.chat_agent_id is not None:
            raise ToolError(
                "Use a native execution credential; legacy agent identities are refused."
            )
        grant = await grants(auth, origin_context_id)
        if (
            grant.adapter.profile != profile
            or str(grant.origin.origin_id) != origin_context_id
            or grant.origin.requester_account_id != auth.account_id
            or grant.origin.resource_scope.tenant_id != str(auth.tenant_id)
            or grant.origin.resource_scope.principal_id != f"mcp:{auth.account_id}"
            or grant.adapter.host.scope != grant.origin.resource_scope
        ):
            raise ScopeViolation(origin_context_id, "native credential grant mismatched the caller")
        return await grant.adapter.offer(grant.origin, kind=kind, target=target)

    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def github_connect(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str
    ) -> NativeOfferReceipt:
        """Offer Connect GitHub for this verified native target; no OAuth is started."""
        return await offer(ctx, origin_context_id, "github", "GitHub")

    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def request_agent_key(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str, key: str
    ) -> NativeOfferReceipt:
        """Offer the shared private-key card for this verified native target.

        Pass only the key name. Secret values never enter tool arguments.
        Submission and expiry scheduling are separate product dependencies.
        """
        return await offer(ctx, origin_context_id, "env", key)

    return names
